"""router-advertisement of eda/common/files/shc_openstack.py against a fake router advertisement service (HTTPS on
the loopback, a certificate made for the test): no service URL = service absent and nothing else; an http:// URL
refused naming the realization key; present and absent idempotent (GET, then PUT only when something changes;
absent drops only what the service holds); every error reason of the service passed through, each with one call (no
retry loop); no answer, a slow answer and an untrusted certificate are ServiceUnavailable; the Keystone token reaches
the service and appears in no result; the IPv4 router interface is the one asked about; the placement teardown asks
the service before the router interface is removed and not while the deletion is blocked."""
import datetime
import http.server
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
sys.path.insert(0, os.path.dirname(__file__))
try:
    import openstack  # noqa: F401
except ImportError:  # stub the SDK so the module imports
    stub = types.ModuleType("openstack")
    stub.exceptions = types.SimpleNamespace(ForbiddenException=PermissionError, NotFoundException=LookupError)
    stub.connect = None
    sys.modules.setdefault("openstack", stub)
import shc_openstack as S  # noqa: E402

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
except ImportError:  # pragma: no cover
    x509 = None

TOKEN = "gAAAAAB-test-token-value-0123456789"
ROUTER, NET, PORT4, PORT6, PROJECT = "rtr-1", "net-1", "port-v4", "port-v6", "proj-platform"
REASONS = [(401, "Unauthenticated"), (403, "NotProjectScoped"), (403, "NotYourPort"), (403, "NotARouterInterface"),
           (404, "NoSuchRouterPort"), (409, "NotAnAdvertisingPort"), (400, "BadRequest"), (502, "OvnUnavailable"),
           (502, "IdentityUnavailable"), (502, "NetworkUnavailable")]


def make_cert(tmp):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "router-advertisement.example")])
    now = datetime.datetime.now(datetime.timezone.utc)
    import ipaddress
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = os.path.join(tmp, "cert.pem"), os.path.join(tmp, "key.pem")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cert_path, key_path


class FakeService:
    """The service of DESIGN-OVN-SHIM §2.1/§4 for the ports it knows; `fail` maps a method to (status, reason);
    `delay` slows every answer."""

    def __init__(self, cert, key):
        self.ports = {PORT4: {"redistribute": ["connected-as-host"], "wanted": False, "restores": 2,
                              "lastRestoredAt": "2026-11-11T00:00:00Z"}}
        self.calls, self.tokens, self.fail, self.delay, self.redirect = [], [], {}, 0, None
        svc = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self, method):
                svc.calls.append((method, self.path))
                svc.tokens.append(self.headers.get("X-Auth-Token"))
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if svc.delay:
                    time.sleep(svc.delay)
                if svc.redirect:
                    self.send_response(302)
                    self.send_header("Location", svc.redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method in svc.fail:
                    st, reason = svc.fail[method]
                    return self.reply(st, {"error": reason, "message": f"{reason} for {self.path}"})
                if self.headers.get("X-Auth-Token") != TOKEN:
                    return self.reply(401, {"error": "Unauthenticated", "message": "token not valid"})
                parts = self.path.strip("/").split("/")
                pid = parts[2] if len(parts) > 2 else ""
                port = svc.ports.get(pid)
                if port is None:
                    return self.reply(404, {"error": "NoSuchRouterPort", "message": f"no lrp-{pid}"})
                red = port["redistribute"]
                if method == "GET":
                    return self.reply(200, {"portId": pid, "lrp": f"lrp-{pid}", "redistribute": red, "nat": "nat" in red,
                                            "wanted": port["wanted"], "restores": port["restores"],
                                            "lastRestoredAt": port["lastRestoredAt"]})
                before = list(red)
                if body.get("enabled"):
                    if "nat" not in red:
                        red.append("nat")
                    port["wanted"] = True
                else:
                    port["redistribute"] = red = [t for t in red if t != "nat"]
                    port["wanted"] = False
                return self.reply(200, {"portId": pid, "lrp": f"lrp-{pid}", "before": before, "after": red,
                                        "changed": before != red})

            def do_GET(self):
                self.handle_any("GET")

            def do_PUT(self):
                self.handle_any("PUT")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.handle_error = lambda request, address: None  # a client that timed out and left
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def site_conn():
    ports = [Obj(id=PORT6, device_id=ROUTER, network_id=NET, device_owner="network:router_interface",
                 fixed_ips=[{"ip_address": "2001:db8::1"}]),
             Obj(id=PORT4, device_id=ROUTER, network_id=NET, device_owner="network:router_interface",
                 fixed_ips=[{"ip_address": "192.0.2.1"}]),
             Obj(id="port-gw", device_id=ROUTER, network_id=NET, device_owner="network:router_gateway",
                 fixed_ips=[{"ip_address": "192.0.2.2"}])]
    net = Obj(ports=lambda **f: [p for p in ports if all(getattr(p, k) == v for k, v in f.items())])
    return Obj(network=net, session=Obj(get_token=lambda: TOKEN), ports=ports)


@unittest.skipIf(x509 is None, "cryptography not installed")
class RouterAdvertisement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shc-ra-test-")
        cls.cert, cls.key = make_cert(cls.tmp)

    def setUp(self):
        self.svc = FakeService(self.cert, self.key)
        self.conn = site_conn()
        self.acct = mock.Mock()
        self.acct.project.return_value = self.conn
        self.acct.verify = self.cert
        self.outputs = []

    def tearDown(self):
        self.svc.stop()
        for out in self.outputs:
            self.assertNotIn(TOKEN, json.dumps(out))

    def call(self, **req):
        base = {"action": "router-advertisement", "clouds_yaml": "/dev/null", "state": "present", "router_id": ROUTER,
                "network_id": NET, "project_id": PROJECT, "service_url": self.svc.url, "site": "site-a", "timeout": 2}
        with mock.patch.object(S, "Account", return_value=self.acct):
            out = S.ACTIONS["router-advertisement"]({**base, **req})
        self.outputs.append(out)
        return out

    def test_no_service_url_means_no_service(self):
        with mock.patch.object(S, "Account", side_effect=AssertionError("no account needed")):
            out = S.router_advertisement({"state": "present", "router_id": ROUTER, "network_id": NET, "service_url": ""})
        self.assertEqual(out["service"], "absent")
        self.assertEqual((out["reason"], out["changed"]), ("", False))
        self.assertEqual(self.svc.calls, [])

    def test_http_is_refused_naming_the_key(self):
        out = self.call(service_url="http://127.0.0.1:9")
        self.assertEqual(out["reason"], "InvalidSetting")
        self.assertIn("placement.routerAdvertisementBySite[site-a].url", out["message"])
        self.assertEqual(self.svc.calls, [])
        self.acct.project.assert_not_called()

    def test_present_then_rerun(self):
        out = self.call()
        self.assertEqual(self.svc.calls, [("GET", f"/v1/router-ports/{PORT4}"), ("PUT", f"/v1/router-ports/{PORT4}/nat")])
        self.assertEqual({k: out[k] for k in ("service", "port_id", "nat", "wanted", "changed", "reason")},
                         {"service": "found", "port_id": PORT4, "nat": True, "wanted": True, "changed": True, "reason": ""})
        self.assertEqual((out["restores"], out["lastRestoredAt"]), (2, "2026-11-11T00:00:00Z"))
        self.assertEqual(self.svc.ports[PORT4]["redistribute"], ["connected-as-host", "nat"])
        self.assertEqual(set(self.svc.tokens), {TOKEN})
        self.acct.project.assert_called_with(PROJECT)
        n = len(self.svc.calls)
        again = self.call()
        self.assertEqual(self.svc.calls[n:], [("GET", f"/v1/router-ports/{PORT4}")])
        self.assertEqual((again["nat"], again["wanted"], again["changed"]), (True, True, False))

    def test_nat_without_the_services_wish_gets_the_marker(self):
        self.svc.ports[PORT4]["redistribute"].append("nat")
        out = self.call()
        self.assertEqual([m for m, _ in self.svc.calls], ["GET", "PUT"])
        self.assertEqual((out["nat"], out["wanted"], out["changed"]), (True, True, False))

    def test_absent_then_rerun(self):
        self.call()
        n = len(self.svc.calls)
        out = self.call(state="absent")
        self.assertEqual([m for m, _ in self.svc.calls[n:]], ["GET", "PUT"])
        self.assertEqual((out["nat"], out["wanted"], out["changed"]), (False, False, True))
        self.assertEqual(self.svc.ports[PORT4]["redistribute"], ["connected-as-host"])
        n = len(self.svc.calls)
        again = self.call(state="absent")
        self.assertEqual([m for m, _ in self.svc.calls[n:]], ["GET"])
        self.assertFalse(again["changed"])

    def test_absent_leaves_a_nat_the_site_set_itself(self):
        self.svc.ports[PORT4]["redistribute"].append("nat")
        out = self.call(state="absent")
        self.assertEqual([m for m, _ in self.svc.calls], ["GET"])
        self.assertTrue(out["nat"])

    def test_every_service_reason_is_passed_through_once(self):
        for method in ("GET", "PUT"):
            for status, reason in REASONS:
                with self.subTest(method=method, reason=reason):
                    self.svc.ports[PORT4].update(redistribute=["connected-as-host"], wanted=False)
                    self.svc.fail = {method: (status, reason)}
                    n = len(self.svc.calls)
                    out = self.call()
                    self.assertEqual(out["reason"], reason)
                    self.assertEqual(out["message"], f"{reason} for /v1/router-ports/{PORT4}" + ("/nat" if method == "PUT" else ""))
                    self.assertEqual([m for m, _ in self.svc.calls[n:]], ["GET"] if method == "GET" else ["GET", "PUT"])
                    self.assertEqual(out["service"], "found")

    def test_an_answer_without_a_reason(self):
        self.svc.fail = {"GET": (500, "")}
        out = self.call()
        self.assertEqual(out["reason"], "ServiceUnavailable")

    def test_untrusted_certificate(self):
        self.acct.verify = True  # the system's trust does not know the test's certificate
        out = self.call()
        self.assertEqual(out["reason"], "ServiceUnavailable")
        self.assertIn("CERTIFICATE", out["message"].upper())
        self.assertEqual(self.svc.calls, [])

    def test_a_redirect_is_never_followed(self):
        # Review R2 M2: a 302 to another listener (plain http) must not carry the token there
        stolen = []

        class Steal(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                stolen.append(dict(self.headers))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_PUT = do_GET

        thief = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Steal)
        threading.Thread(target=thief.serve_forever, daemon=True).start()
        try:
            for target in (f"http://127.0.0.1:{thief.server_address[1]}/steal", f"{self.svc.url}/v1/router-ports/{PORT4}"):
                self.svc.redirect = target
                out = self.call()
                self.assertEqual(out["reason"], "ServiceUnavailable")
                self.assertIn("redirect", out["message"])
                self.assertIn(self.svc.url, out["message"])
            self.assertEqual(stolen, [])
        finally:
            thief.shutdown()
            thief.server_close()

    def test_certificate_verification_off_is_refused(self):
        # Review R2 M2: whatever the site credential says, the token never goes over an unchecked connection
        for off in (False, "false", "False", "no"):
            self.acct.verify = off
            out = self.call()
            self.assertEqual(out["reason"], "InvalidSetting", off)
            self.assertIn("verify: false", out["message"])
            self.assertIn("site-a", out["message"])
        self.assertEqual(self.svc.calls, [])

    def test_the_token_goes_only_to_the_configured_origin(self):
        with self.assertRaises(ValueError):
            S._ra_http("GET", "https://198.51.100.7:8443/v1/router-ports/x", TOKEN, self.cert, 1,
                       origin=S._ra_origin(self.svc.url))
        self.assertEqual(self.svc.calls, [])
        # an odd port id cannot move the request elsewhere (it is one quoted path segment)
        self.conn.ports[1].id = "../../x?y#z"
        self.call()
        self.assertTrue(all(p.startswith("/v1/router-ports/..%2F..%2Fx%3Fy%23z") for _, p in self.svc.calls), self.svc.calls)

    def test_no_answer(self):
        self.svc.stop()
        out = self.call()
        self.assertEqual(out["reason"], "ServiceUnavailable")
        self.svc = FakeService(self.cert, self.key)

    def test_every_call_times_out(self):
        self.svc.delay = 2
        t0 = time.monotonic()
        out = self.call(timeout=0.5)
        self.assertLess(time.monotonic() - t0, 1.9)
        self.assertEqual(out["reason"], "ServiceUnavailable")

    def test_no_ipv4_interface(self):
        self.conn.ports[:] = [p for p in self.conn.ports if p.id != PORT4]
        out = self.call()
        self.assertEqual(out["reason"], "NoRouterInterface")
        self.assertEqual(self.svc.calls, [])

    def test_no_ipv4_interface_and_absent_is_done(self):
        # gate F3: an IPv6-only placement's router has no IPv4 interface: nothing can carry nat, nothing to drop
        self.conn.ports[:] = [p for p in self.conn.ports if p.id != PORT4]
        out = self.call(state="absent")
        self.assertEqual((out["reason"], out["nat"], out["wanted"], out["changed"]), ("", False, False, False))
        self.assertEqual(self.svc.calls, [])

    def test_unknown_port_is_reported(self):
        self.svc.ports.clear()
        out = self.call()
        self.assertEqual((out["reason"], out["port_id"]), ("NoSuchRouterPort", PORT4))
        self.assertEqual(len(self.svc.calls), 1)


class TeardownOrder(unittest.TestCase):
    """placement-network state absent: the service is asked (absent) after the busy check and before the router
    interfaces are removed; never while the deletion is blocked; a failure does not stop the deletion."""

    def setUp(self):
        import test_shc_openstack_placement as P
        self.P = P
        self.conn = P.FakeConn()
        P.run(self.conn, use={"routerAttachments": True})

    def absent(self, ra_result=None, ra_raises=None, **over):
        seen = []

        def fake(acct, req):
            seen.append((dict(req), len([op for op, _ in self.conn.calls if op == "remove_interface_from_router"])))
            if ra_raises:
                raise ra_raises
            return ra_result or {"service": "found", "nat": False, "wanted": False, "reason": ""}
        with mock.patch.object(S, "_router_advertisement", side_effect=fake):
            out = self.P.run(self.conn, state="absent", **over)
        return out, seen

    def test_asked_before_the_interface_goes(self):
        out, seen = self.absent(routerAdvertisement={"service_url": "https://ra.example", "site": "site-a"})
        self.assertEqual(len(seen), 1)
        req, removed_before = seen[0]
        self.assertEqual(removed_before, 0)
        self.assertEqual((req["state"], req["router_id"], req["network_id"], req["project_id"]),
                         ("absent", "rtr-4", "net-1", "proj-platform"))
        self.assertEqual(out["routerAdvertisement"]["service"], "found")
        self.assertEqual(self.conn.network.rtrs, [])

    def test_not_asked_while_blocked(self):
        net = self.conn.network.nets[0]
        self.conn.network.plist.append(self.P.Obj(id="p-fip", device_id="fip-1", device_owner="network:floatingip",
                                                  network_id=net.id, project_id="", name="",
                                                  fixed_ips=[{"ip_address": "192.0.2.15"}]))
        out, seen = self.absent(routerAdvertisement={"service_url": "https://ra.example"})
        self.assertEqual(seen, [])
        self.assertIn("blocked", out)

    def test_without_a_service_nothing_is_asked(self):
        out, seen = self.absent()
        self.assertEqual(seen, [])
        self.assertNotIn("routerAdvertisement", out)

    def test_a_failing_service_does_not_stop_the_deletion(self):
        out, seen = self.absent(ra_raises=RuntimeError("boom"), routerAdvertisement={"service_url": "https://ra.example"})
        self.assertEqual(out["routerAdvertisement"]["reason"], "ServiceUnavailable")
        self.assertEqual(self.conn.network.nets, [])


if __name__ == "__main__":
    unittest.main()
