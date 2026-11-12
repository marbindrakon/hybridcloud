"""Contract between the platform's client (shc_openstack.py router-advertisement, stream S3) and the router
advertisement service (site-components/router-advertisement/router_advertisement.py, stream S6): the REAL service
program (its HTTP server and handler, its transaction steps on the fake OVN database and its fake Keystone/Neutron, as
the service's own tests build them) served over TLS on the loopback, called by the REAL client code. Every reason the
service can return reaches the client's result as the specification says (DESIGN-OVN-SHIM §2.1, §3, §4): present
holds nat and the service's wish; absent drops only what the service holds, and NotAnAdvertisingPort (no
redistribute option, also on GET and on {"enabled": false}) is "nothing to do" for absent and a failure for present;
the guard's restores reach the client's record; NotFound, MethodNotAllowed and InternalError are failures with the
service's reason; the Keystone token appears in no result. No network beyond the loopback."""
import json
import os
import sys
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_router_advertisement_service as SVC  # noqa: E402  (the service module and its fakes)
import test_shc_openstack_router_advertisement as CLI  # noqa: E402  (the client module, its stubs, make_cert)

RA, S = SVC.RA, CLI.S
ROUTER, NET = "router-1", "net-1"


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def client_conn(port_id, token=SVC.TOKEN):
    """The site account scoped to the router's project, as the client sees it: the router's IPv4 interface port."""
    ports = [Obj(id=port_id, device_id=ROUTER, network_id=NET, device_owner="network:router_interface",
                 fixed_ips=[{"ip_address": "192.0.2.1"}])]
    net = Obj(ports=lambda **f: [p for p in ports if all(getattr(p, k) == v for k, v in f.items())])
    return Obj(network=net, session=Obj(get_token=lambda: token))


@unittest.skipIf(CLI.x509 is None, "cryptography not installed")
class ClientAgainstTheService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp(prefix="shc-ra-contract-")
        cls.cert, cls.key = CLI.make_cert(cls.tmp)

    def setUp(self):
        self.db = SVC.site()
        self.svc, self.store, self.guard = SVC.service(self.db)
        self.server = RA.build_server(self.svc, "127.0.0.1", 0, self.cert, self.key)
        self.url = f"https://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.outputs = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for out in self.outputs:
            for secret in SVC.SECRETS:
                self.assertNotIn(secret, json.dumps(out))

    def call(self, port_id, state="present", token=SVC.TOKEN, url=None):
        acct = mock.Mock()
        acct.project.return_value = client_conn(port_id, token)
        acct.verify = self.cert
        req = {"action": "router-advertisement", "clouds_yaml": "/dev/null", "state": state, "router_id": ROUTER,
               "network_id": NET, "project_id": SVC.PROJECT, "service_url": url or self.url, "site": "site-a",
               "timeout": 5}
        with mock.patch.object(S, "Account", return_value=acct):
            out = S.ACTIONS["router-advertisement"](req)
        self.outputs.append(out)
        return out

    def tokens(self, port_id):
        return RA.parse_tokens(self.db.options("lrp-" + port_id).get(RA.OPTION, ""))

    def test_present_holds_nat_and_the_wish_then_absent_drops_it(self):
        out = self.call(SVC.PORT)
        self.assertEqual({k: out[k] for k in ("service", "port_id", "nat", "wanted", "changed", "reason")},
                         {"service": "found", "port_id": SVC.PORT, "nat": True, "wanted": True, "changed": True,
                          "reason": ""})
        self.assertEqual(self.tokens(SVC.PORT), ["connected-as-host", "nat"])
        again = self.call(SVC.PORT)
        self.assertEqual((again["nat"], again["wanted"], again["changed"], again["reason"]), (True, True, False, ""))
        gone = self.call(SVC.PORT, state="absent")
        self.assertEqual((gone["nat"], gone["wanted"], gone["changed"], gone["reason"]), (False, False, True, ""))
        self.assertEqual(self.tokens(SVC.PORT), ["connected-as-host"])
        self.assertEqual(self.call(SVC.PORT, state="absent")["changed"], False)

    def test_the_guards_restores_reach_the_record(self):
        self.call(SVC.PORT)
        self.db.external_write("lrp-" + SVC.PORT, RA.OPTION, "connected-as-host")   # Neutron rewrites the option
        SVC.drain(self.guard)
        self.assertEqual(self.tokens(SVC.PORT), ["connected-as-host", "nat"])
        out = self.call(SVC.PORT)
        self.assertEqual((out["restores"], out["reason"], out["changed"]), (1, "", False))
        self.assertTrue(out["lastRestoredAt"])

    def test_absent_leaves_a_nat_the_site_set_itself(self):
        self.db.external_write("lrp-" + SVC.PORT, RA.OPTION, "connected-as-host,nat")
        out = self.call(SVC.PORT, state="absent")
        self.assertEqual((out["nat"], out["wanted"], out["changed"], out["reason"]), (True, False, False, ""))
        self.assertEqual(self.tokens(SVC.PORT), ["connected-as-host", "nat"])

    def test_a_port_without_the_option(self):
        """409 NotAnAdvertisingPort: for absent nothing to do; for present a failure the placement reports."""
        out = self.call(SVC.NO_OPT, state="absent")
        self.assertEqual((out["reason"], out["nat"], out["wanted"]), ("", False, False))
        out = self.call(SVC.NO_OPT)
        self.assertEqual(out["reason"], "NotAnAdvertisingPort")
        self.assertTrue(out["message"])
        self.assertNotIn(RA.OPTION, self.db.options("lrp-" + SVC.NO_OPT))   # the service never creates the option

    def test_every_refusal_reaches_the_client(self):
        cases = [
            (dict(port_id=SVC.FOREIGN), "NotYourPort"),
            (dict(port_id=SVC.GW_PORT), "NotARouterInterface"),
            (dict(port_id=SVC.NO_LRP), "NoSuchRouterPort"),
            (dict(port_id=SVC.PORT, token="gAAAAABnotAKnownToken-000="), "Unauthenticated"),
            (dict(port_id=SVC.PORT, token=SVC.TOKEN_UNSCOPED), "NotProjectScoped"),
            (dict(port_id="Not-A-Uuid"), "BadRequest"),
            (dict(port_id=SVC.PORT, url=None), ""),
        ]
        for kw, want in cases:
            for state in ("present", "absent") if want else ("present",):
                with self.subTest(want=want, state=state):
                    out = self.call(state=state, **kw)
                    self.assertEqual(out["reason"], want)
                    if want:
                        self.assertTrue(out["message"])

    def test_dependency_failures(self):
        self.store.up = False
        self.assertEqual(self.call(SVC.PORT)["reason"], "OvnUnavailable")
        self.store.up = True
        self.svc.openstack.identity_down = True
        self.assertEqual(self.call(SVC.PORT)["reason"], "IdentityUnavailable")
        self.svc.openstack.identity_down = False
        self.svc.openstack.network_down = True
        self.assertEqual(self.call(SVC.PORT)["reason"], "NetworkUnavailable")

    def test_the_services_own_reasons(self):
        # a URL with a path the service does not serve: NotFound, a failure with the service's reason
        out = self.call(SVC.PORT, url=self.url + "/prefix")
        self.assertEqual(out["reason"], "NotFound")
        # an unexpected fault in the service: InternalError without detail
        with mock.patch.object(self.svc.openstack, "validate", side_effect=RuntimeError("boom")):
            out = self.call(SVC.PORT)
        self.assertEqual(out["reason"], "InternalError")
        self.assertNotIn("boom", out["message"])
        # the client only sends GET and PUT; the service refuses anything else with MethodNotAllowed
        status, body = self.svc.handle("POST", f"/v1/router-ports/{SVC.PORT}/nat", {"X-Auth-Token": SVC.TOKEN}, b"{}")
        self.assertEqual((status, body["error"]), (405, "MethodNotAllowed"))

    def test_the_reasons_the_client_documents_are_the_services(self):
        doc = S.router_advertisement.__doc__
        for reason in ("Unauthenticated", "NotProjectScoped", "NotYourPort", "NotARouterInterface", "NoSuchRouterPort",
                       "NotAnAdvertisingPort", "BadRequest", "OvnUnavailable", "IdentityUnavailable",
                       "NetworkUnavailable", "NotFound", "MethodNotAllowed", "InternalError"):
            self.assertIn(reason, doc)


if __name__ == "__main__":
    unittest.main()
