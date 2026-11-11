"""The router advertisement service (site-components/router-advertisement/router_advertisement.py) with its OVN
database, Keystone and Neutron replaced by fakes: every row of the API and error tables, authorization, the token
list, the marker and the guard (events, periodic pass, rate limit, restart), dependency failures, the settings, the
logs (no token in them), concurrency and shutdown. The fake database runs the program's own transaction steps with
OVSDB's optimistic concurrency (verify, TRY_AGAIN, re-run), so a concurrent writer is part of the tests.

No network: the HTTP tests listen on the loopback only. The program's ovsdbapp class is tested with stand-in
modules (the test environment has no ovsdbapp)."""
import http.client
import importlib.util
import io
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
PATH = os.path.join(ROOT, "site-components/router-advertisement/router_advertisement.py")
spec = importlib.util.spec_from_file_location("router_advertisement", PATH)
RA = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RA)
RA.LOG.addHandler(logging.NullHandler())  # nothing on stderr; LogCapture reads the lines

PORT = "0b6f3f9e-2c1d-4a8e-9f00-1a2b3c4d5e6f"
PORT2 = "7c9d2e10-5b4a-4f3e-8d2c-0123456789ab"
GW_PORT = "11111111-2222-4333-8444-555555555555"
FOREIGN = "99999999-8888-4777-8666-555555555555"
NO_OPT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
NO_LRP = "12345678-90ab-4cde-8f01-234567890abc"
TOKEN = "gAAAAABexampleTOKENvalue-abc_123="
TOKEN_OTHER = "gAAAAABotherProjectToken-xyz_789="
TOKEN_UNSCOPED = "gAAAAABunscopedTokenValue-000="
PROJECT = "f0e1d2c3b4a5968778695a4b3c2d1e0f"
OTHER_PROJECT = "0123456789abcdef0123456789abcdef"
USER = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SECRETS = (TOKEN, TOKEN_OTHER, TOKEN_UNSCOPED)


# ---------------------------------------------------------------------------------------------------------------
# Fakes

class FakeRow:
    """A row as a transaction step sees it: a copy of the options, verify/setkey/delkey recorded for the commit."""

    def __init__(self, name, options, version):
        self.name = name
        self.options = dict(options)
        self.version = version
        self.verified = False
        self.ops = []

    def verify(self, column):
        assert column == "options"
        self.verified = True

    def setkey(self, column, key, value):
        assert column == "options"
        self.ops.append(("set", key, value))

    def delkey(self, column, key):
        assert column == "options"
        self.ops.append(("del", key))


class FakeDatabase:
    """The Logical_Router_Port table with OVSDB's commit semantics: a transaction whose verified rows changed since
    they were read returns TRY_AGAIN and is re-run from the read; mutations touch single keys."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {}
        self.versions = {}
        self.listeners = []
        self.retries = 0
        self.commits = 0
        self.before_commit = None  # called once, between a transaction's read and its commit

    def add(self, name, options):
        with self.lock:
            self.rows[name] = dict(options)
            self.versions[name] = 1

    def options(self, name):
        with self.lock:
            return dict(self.rows[name])

    def external_write(self, name, key, value=None, delete=False):
        """Another writer (Neutron, an administrator): one key, its own transaction."""
        with self.lock:
            if delete:
                self.rows[name].pop(key, None)
            else:
                self.rows[name][key] = value
            self.versions[name] += 1
        self._notify([name])

    def _notify(self, names):
        for name in names:
            for listener in list(self.listeners):
                listener(name)

    def transact(self, step):
        for _ in range(20):
            with self.lock:
                snapshot = [FakeRow(n, o, self.versions[n]) for n, o in self.rows.items()]
            result = step(snapshot)
            hook, self.before_commit = self.before_commit, None
            if hook is not None:
                hook()
            with self.lock:
                stale = [r for r in snapshot if r.verified and self.versions.get(r.name) != r.version]
                if stale:
                    self.retries += 1
                    continue
                changed = []
                for r in snapshot:
                    if not r.ops:
                        continue
                    for op in r.ops:
                        if op[0] == "set":
                            self.rows[r.name][op[1]] = op[2]
                        else:
                            self.rows[r.name].pop(op[1], None)
                    self.versions[r.name] += 1
                    changed.append(r.name)
                self.commits += 1
            self._notify(changed)
            return result
        raise RuntimeError("too many retries")


class FakePortStore:
    """The program's store interface on the fake database, running the program's own transaction steps."""

    def __init__(self, db, events=True):
        self.db = db
        self.events = events
        self.up = True
        self.seqno = 1
        self.calls = []

    def start(self, on_change=None):
        if self.events and on_change is not None:
            self.db.listeners.append(on_change)

    def stop(self, timeout=5.0):
        pass

    def connected(self):
        return self.up

    def generation(self):
        return self.seqno

    def _run(self, step, *args):
        self.calls.append(step)
        if not self.up:
            raise RA.ovn_unavailable()
        return self.db.transact(lambda rows: step(rows, *args))

    def read_port(self, lrp):
        def read(rows, name):
            row = RA.find_row(rows, name)
            if row is None:
                raise RA.no_such_router_port(name)
            return RA.port_state(row)
        return self._run(read, lrp)

    def set_nat(self, lrp, enabled):
        return self._run(lambda rows, n, on: RA.step_set_nat(RA.find_row(rows, n), n, on), lrp, enabled)

    def restore_nat(self, lrp):
        return self._run(lambda rows, n: RA.step_restore(RA.find_row(rows, n), n), lrp)

    def marked_ports(self):
        return self._run(RA.scan_marked)


class FakeOpenStack:
    def __init__(self):
        self.tokens = {TOKEN: {"user_id": USER, "project_id": PROJECT},
                       TOKEN_OTHER: {"user_id": "u2", "project_id": OTHER_PROJECT},
                       TOKEN_UNSCOPED: {"user_id": USER, "project_id": None}}
        owner = "network:router_interface"
        self.ports = {PORT: {"id": PORT, "project_id": PROJECT, "device_owner": owner},
                      PORT2: {"id": PORT2, "tenant_id": PROJECT, "device_owner": owner},
                      NO_OPT: {"id": NO_OPT, "project_id": PROJECT, "device_owner": owner},
                      NO_LRP: {"id": NO_LRP, "project_id": PROJECT, "device_owner": owner},
                      GW_PORT: {"id": GW_PORT, "project_id": PROJECT, "device_owner": "network:router_gateway"},
                      FOREIGN: {"id": FOREIGN, "project_id": OTHER_PROJECT, "device_owner": owner}}
        self.hidden = {FOREIGN}  # Neutron shows these only to their own project
        self.identity_down = False
        self.network_down = False
        self.delay = 0.0

    def validate(self, token):
        if self.delay:
            time.sleep(self.delay)
        if self.identity_down:
            raise RA.identity_unavailable()
        if token not in self.tokens:
            raise RA.unauthenticated("Keystone does not accept the token.")
        return dict(self.tokens[token])

    def port(self, token, port_id):
        if self.network_down:
            raise RA.network_unavailable()
        port = self.ports.get(port_id)
        if port is None:
            return None
        project = self.tokens[token]["project_id"]
        if port_id in self.hidden and (port.get("project_id") or port.get("tenant_id")) != project:
            return None
        return dict(port)


def site():
    db = FakeDatabase()
    db.add("lrp-" + PORT, {RA.OPTION: "connected-as-host", "dynamic-routing-advertise": "false"})
    db.add("lrp-" + PORT2, {RA.OPTION: "static, connected-as-host,static"})
    db.add("lrp-" + NO_OPT, {"gateway_mtu": "1400"})
    db.add("lrp-" + GW_PORT, {RA.OPTION: "connected-as-host"})
    db.add("lrp-" + FOREIGN, {RA.OPTION: "connected-as-host"})
    return db


def service(db, events=True, interval=60.0, clock=None):
    store = FakePortStore(db, events=events)
    guard = RA.Guard(store, interval=interval, clock=clock or time.time)
    store.start(on_change=guard.notify)
    return RA.RouterAdvertisementService(store, FakeOpenStack(), guard), store, guard


def put(svc, port_id, enabled, token=TOKEN):
    body = json.dumps({"enabled": enabled}).encode()
    return svc.handle("PUT", "/v1/router-ports/%s/nat" % port_id, {"X-Auth-Token": token}, body)


def get(svc, port_id, token=TOKEN):
    return svc.handle("GET", "/v1/router-ports/%s" % port_id, {"X-Auth-Token": token}, None)


def describe_exc(exc):
    return "%s: %s" % (type(exc).__name__, exc)


def drain(guard, turns=5):
    """Run the guard's queued work synchronously (as its thread would)."""
    for _ in range(turns):
        guard.run_once(wait=0.01)


class LogCapture:
    def __init__(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    def __enter__(self):
        RA.LOG.addHandler(self.handler)
        self.level = RA.LOG.level
        RA.LOG.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        RA.LOG.removeHandler(self.handler)
        RA.LOG.setLevel(self.level)

    @property
    def text(self):
        return self.stream.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.db = site()
        self.svc, self.store, self.guard = service(self.db)
        self.logs = LogCapture().__enter__()
        self.addCleanup(self.logs.__exit__)

    def tearDown(self):
        for secret in SECRETS:
            self.assertNotIn(secret, self.logs.text, "a token reached the log")

    def assertError(self, result, status, reason):
        self.assertEqual(result[0], status, result)
        self.assertEqual(result[1]["error"], reason, result)
        self.assertTrue(result[1]["message"])
        for secret in SECRETS:
            self.assertNotIn(secret, json.dumps(result[1]))


# ---------------------------------------------------------------------------------------------------------------
# The token list

class TokenListTest(unittest.TestCase):
    def test_parse_keeps_order_drops_blanks_spaces_and_duplicates(self):
        self.assertEqual(RA.parse_tokens("static, connected-as-host,,static ,nat"),
                         ["static", "connected-as-host", "nat"])
        self.assertEqual(RA.parse_tokens(""), [])
        self.assertEqual(RA.parse_tokens(None), [])

    def test_serialise_is_comma_separated_without_spaces(self):
        self.assertEqual(RA.serialise_tokens(["static", "connected-as-host", "nat"]), "static,connected-as-host,nat")

    def test_nat_appended_at_the_end_and_removed_in_place(self):
        self.assertEqual(RA.with_nat(["connected-as-host"], True), ["connected-as-host", "nat"])
        self.assertEqual(RA.with_nat(["nat", "connected-as-host"], True), ["connected-as-host", "nat"])
        self.assertEqual(RA.with_nat(["static", "nat", "connected-as-host"], False), ["static", "connected-as-host"])
        self.assertEqual(RA.with_nat(["connected-as-host"], False), ["connected-as-host"])


# ---------------------------------------------------------------------------------------------------------------
# The API table

class ApiTest(Base):
    def test_healthz_connected_and_unreachable(self):
        status, body = self.svc.handle("GET", "/healthz", {}, None)
        self.assertEqual((status, body["ok"], body["ovn"]), (200, True, "connected"))
        self.assertEqual(body["guard"], {"wanted": 0, "restoredSinceStart": 0, "suspended": []})
        self.store.up = False
        status, body = self.svc.handle("GET", "/healthz", {}, None)
        self.assertEqual((status, body["ovn"]), (200, "unreachable"))

    def test_get_port(self):
        status, body = get(self.svc, PORT)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"portId": PORT, "lrp": "lrp-" + PORT, "redistribute": ["connected-as-host"],
                                "nat": False, "wanted": False, "restores": 0, "lastRestoredAt": None})

    def test_put_enable_appends_nat_and_marks(self):
        status, body = put(self.svc, PORT, True)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"portId": PORT, "lrp": "lrp-" + PORT, "before": ["connected-as-host"],
                                "after": ["connected-as-host", "nat"], "changed": True})
        opts = self.db.options("lrp-" + PORT)
        self.assertEqual(opts[RA.OPTION], "connected-as-host,nat")
        self.assertEqual(opts[RA.MARKER], "wanted")
        self.assertEqual(opts["dynamic-routing-advertise"], "false", "other option keys are kept")
        self.assertTrue(get(self.svc, PORT)[1]["nat"])
        self.assertTrue(get(self.svc, PORT)[1]["wanted"])

    def test_put_keeps_other_tokens_and_their_order_and_removes_duplicates(self):
        status, body = put(self.svc, PORT2, True)
        self.assertEqual(body["before"], ["static", "connected-as-host"])
        self.assertEqual(body["after"], ["static", "connected-as-host", "nat"])
        self.assertEqual(self.db.options("lrp-" + PORT2)[RA.OPTION], "static,connected-as-host,nat")

    def test_put_disable_removes_token_and_marker(self):
        put(self.svc, PORT, True)
        status, body = put(self.svc, PORT, False)
        self.assertEqual(status, 200)
        self.assertEqual((body["before"], body["after"], body["changed"]),
                         (["connected-as-host", "nat"], ["connected-as-host"], True))
        opts = self.db.options("lrp-" + PORT)
        self.assertEqual(opts[RA.OPTION], "connected-as-host")
        self.assertNotIn(RA.MARKER, opts)

    def test_idempotent_enable(self):
        put(self.svc, PORT, True)
        commits = self.db.commits
        status, body = put(self.svc, PORT, True)
        self.assertEqual((status, body["changed"], body["before"], body["after"]),
                         (200, False, ["connected-as-host", "nat"], ["connected-as-host", "nat"]))
        self.assertEqual(self.db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host,nat")
        self.assertEqual(self.db.versions["lrp-" + PORT], 2, "no second write")
        self.assertEqual(self.db.commits, commits + 1)  # an empty transaction, nothing written

    def test_idempotent_disable(self):
        status, body = put(self.svc, PORT, False)
        self.assertEqual((status, body["changed"], body["before"], body["after"]),
                         (200, False, ["connected-as-host"], ["connected-as-host"]))
        self.assertEqual(self.db.versions["lrp-" + PORT], 1)
        status, body = put(self.svc, PORT, False)
        self.assertFalse(body["changed"])

    def test_enable_on_a_port_that_has_nat_but_no_marker_marks_it(self):
        self.db.external_write("lrp-" + PORT, RA.OPTION, "connected-as-host,nat")
        status, body = put(self.svc, PORT, True)
        self.assertEqual((body["before"], body["after"], body["changed"]),
                         (["connected-as-host", "nat"], ["connected-as-host", "nat"], True))
        self.assertEqual(self.db.options("lrp-" + PORT)[RA.MARKER], "wanted")

    def test_write_is_logged_once_with_ids_and_lists(self):
        put(self.svc, PORT, True)
        lines = [ln for ln in self.logs.text.splitlines() if "write:" in ln]
        self.assertEqual(len(lines), 1)
        for part in (USER, PROJECT, PORT, "before connected-as-host", "after connected-as-host,nat"):
            self.assertIn(part, lines[0])
        put(self.svc, PORT, True)
        self.assertEqual(len([ln for ln in self.logs.text.splitlines() if "write:" in ln]), 1, "no-op not logged")

    # errors ------------------------------------------------------------------------------------------------------
    def test_401_without_token(self):
        self.assertError(self.svc.handle("GET", "/v1/router-ports/%s" % PORT, {}, None), 401, "Unauthenticated")
        self.assertError(put(self.svc, PORT, True, token=""), 401, "Unauthenticated")

    def test_401_invalid_token(self):
        self.assertError(get(self.svc, PORT, token="gAAAAABnotKnownToKeystone"), 401, "Unauthenticated")
        self.assertError(get(self.svc, PORT, token="bad token\r\nX-Injected: 1"), 401, "Unauthenticated")
        self.assertError(get(self.svc, PORT, token="x" * 9000), 401, "Unauthenticated")

    def test_403_unscoped_token(self):
        self.assertError(put(self.svc, PORT, True, token=TOKEN_UNSCOPED), 403, "NotProjectScoped")

    def test_403_another_projects_port(self):
        # hidden from the caller by Neutron
        self.assertError(put(self.svc, FOREIGN, True), 403, "NotYourPort")
        # visible (an admin token sees every port), but of another project
        self.assertError(put(self.svc, PORT, True, token=TOKEN_OTHER), 403, "NotYourPort")
        self.assertNotIn("nat", self.db.options("lrp-" + PORT)[RA.OPTION])
        self.assertNotIn("nat", self.db.options("lrp-" + FOREIGN)[RA.OPTION])

    def test_403_port_neutron_does_not_know(self):
        self.assertError(get(self.svc, "deadbeef-0000-4000-8000-000000000000"), 403, "NotYourPort")

    def test_403_not_a_router_interface(self):
        self.assertError(put(self.svc, GW_PORT, True), 403, "NotARouterInterface")
        self.assertEqual(self.db.options("lrp-" + GW_PORT)[RA.OPTION], "connected-as-host")

    def test_404_no_such_router_port(self):
        self.assertError(get(self.svc, NO_LRP), 404, "NoSuchRouterPort")
        self.assertError(put(self.svc, NO_LRP, True), 404, "NoSuchRouterPort")

    def test_409_not_an_advertising_port_and_the_option_is_never_created(self):
        self.assertError(get(self.svc, NO_OPT), 409, "NotAnAdvertisingPort")
        self.assertError(put(self.svc, NO_OPT, True), 409, "NotAnAdvertisingPort")
        self.assertError(put(self.svc, NO_OPT, False), 409, "NotAnAdvertisingPort")
        self.assertEqual(self.db.options("lrp-" + NO_OPT), {"gateway_mtu": "1400"})

    def test_400_malformed_ids(self):
        for bad in (PORT.upper(), "not-a-uuid", PORT + "0", "..%2F..%2Fetc", "' or 1=1 --", PORT[:-1] + "g"):
            self.assertError(get(self.svc, bad), 400, "BadRequest")
            self.assertError(put(self.svc, bad, True), 400, "BadRequest")
        self.assertEqual(self.store.calls, [], "nothing reached the database")

    def test_400_malformed_bodies(self):
        path = "/v1/router-ports/%s/nat" % PORT
        for body in (None, b"", b"not json", b"[]", b'{"enabled": "true"}', b'{"enabled": 1}', b'{}',
                     b'{"enabled": true, "other": 1}', b"\xff\xfe", b'{"enabled": true}' + b" " * 5000):
            self.assertError(self.svc.handle("PUT", path, {"X-Auth-Token": TOKEN}, body), 400, "BadRequest")
        self.assertEqual(self.store.calls, [])

    def test_502_dependencies(self):
        self.svc.openstack.identity_down = True
        self.assertError(put(self.svc, PORT, True), 502, "IdentityUnavailable")
        self.svc.openstack.identity_down = False
        self.svc.openstack.network_down = True
        self.assertError(put(self.svc, PORT, True), 502, "NetworkUnavailable")
        self.svc.openstack.network_down = False
        self.store.up = False
        self.assertError(put(self.svc, PORT, True), 502, "OvnUnavailable")
        self.assertError(get(self.svc, PORT), 502, "OvnUnavailable")

    def test_unknown_paths_and_methods(self):
        self.assertError(self.svc.handle("GET", "/v1/router-ports", {}, None), 404, "NotFound")
        self.assertError(self.svc.handle("GET", "/v1/router-ports/%s/nat/x" % PORT, {}, None), 404, "NotFound")
        self.assertError(self.svc.handle("DELETE", "/v1/router-ports/%s" % PORT, {}, None), 405, "MethodNotAllowed")
        self.assertError(self.svc.handle("GET", "/v1/router-ports/%s/nat" % PORT, {}, None), 405,
                         "MethodNotAllowed")
        self.assertError(self.svc.handle("POST", "/healthz", {}, None), 405, "MethodNotAllowed")

    def test_header_lookup_is_case_insensitive(self):
        status, _ = self.svc.handle("GET", "/v1/router-ports/%s" % PORT, {"x-auth-token": TOKEN}, None)
        self.assertEqual(status, 200)

    def test_unexpected_failure_is_500_without_detail(self):
        def boom(*a):
            raise KeyError("internal detail " + TOKEN)
        self.store.read_port = boom
        status, body = get(self.svc, PORT)
        self.assertEqual((status, body["error"]), (500, "InternalError"))
        self.assertNotIn("internal detail", body["message"])
        self.logs.stream.seek(0)
        self.logs.stream.truncate(0)  # the test's own exception text carried the token on purpose


# ---------------------------------------------------------------------------------------------------------------
# Concurrency on the database

class TransactionTest(Base):
    def test_concurrent_neutron_rewrite_is_not_lost(self):
        """Neutron writes the option between the API's read and its commit: the commit is refused, re-run on the
        new value, and both writes survive."""
        lrp = "lrp-" + PORT
        self.db.before_commit = lambda: self.db.external_write(lrp, RA.OPTION, "static,connected-as-host")
        status, body = put(self.svc, PORT, True)
        self.assertEqual(status, 200)
        self.assertEqual(self.db.retries, 1)
        self.assertEqual(self.db.options(lrp)[RA.OPTION], "static,connected-as-host,nat")
        self.assertEqual(body["before"], ["static", "connected-as-host"])

    def test_step_verifies_before_writing(self):
        row = FakeRow("lrp-" + PORT, {RA.OPTION: "connected-as-host"}, 1)
        RA.step_set_nat(row, row.name, True)
        self.assertTrue(row.verified)
        self.assertEqual(row.ops, [("set", RA.OPTION, "connected-as-host,nat"), ("set", RA.MARKER, "wanted")])
        row = FakeRow("lrp-" + PORT, {RA.OPTION: "connected-as-host,nat", RA.MARKER: "wanted"}, 1)
        RA.step_set_nat(row, row.name, False)
        self.assertEqual(row.ops, [("set", RA.OPTION, "connected-as-host"), ("del", RA.MARKER)])

    def test_concurrent_api_requests(self):
        errors = []

        def worker(port_id, enabled):
            try:
                status, _ = put(self.svc, port_id, enabled)
                if status != 200:
                    errors.append(status)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(PORT if i % 2 else PORT2, True)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host,nat")
        self.assertEqual(self.db.options("lrp-" + PORT2)[RA.OPTION], "static,connected-as-host,nat")
        self.assertEqual(len([ln for ln in self.logs.text.splitlines() if "write:" in ln]), 2)


# ---------------------------------------------------------------------------------------------------------------
# The guard

class GuardTest(Base):
    def neutron_resets(self, port_id=PORT):
        self.db.external_write("lrp-" + port_id, RA.OPTION, "connected-as-host")

    def test_event_path_restores_after_a_neutron_rewrite(self):
        put(self.svc, PORT, True)
        drain(self.guard)  # first full pass
        self.neutron_resets()
        self.assertEqual(self.db.options("lrp-" + PORT)[RA.MARKER], "wanted", "Neutron keeps the marker")
        drain(self.guard)
        self.assertEqual(self.db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host,nat")
        lines = [ln for ln in self.logs.text.splitlines() if "guard: restored nat on lrp-" + PORT in ln]
        self.assertEqual(len(lines), 1)
        _, body = get(self.svc, PORT)
        self.assertEqual(body["restores"], 1)
        self.assertRegex(body["lastRestoredAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.svc.handle("GET", "/healthz", {}, None)[1]["guard"],
                         {"wanted": 1, "restoredSinceStart": 1, "suspended": []})

    def test_periodic_pass_restores_without_events(self):
        now = [1000.0]
        db = site()
        svc, store, guard = service(db, events=False, interval=60, clock=lambda: now[0])
        put(svc, PORT, True)
        drain(guard)
        db.external_write("lrp-" + PORT, RA.OPTION, "connected-as-host")
        drain(guard)
        self.assertEqual(db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host", "no event, not due yet")
        now[0] += 61
        drain(guard)
        self.assertEqual(db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host,nat")

    def test_reconnect_triggers_a_full_pass(self):
        now = [1000.0]
        db = site()
        svc, store, guard = service(db, events=False, interval=3600, clock=lambda: now[0])
        put(svc, PORT, True)
        drain(guard)
        db.external_write("lrp-" + PORT, RA.OPTION, "connected-as-host")
        store.up = False
        drain(guard)
        store.up = True
        store.seqno += 2
        drain(guard)
        self.assertEqual(db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host,nat")

    def test_marked_port_without_the_option_is_left_alone_and_logged_once(self):
        put(self.svc, PORT, True)
        drain(self.guard)
        self.db.external_write("lrp-" + PORT, RA.OPTION, delete=True)
        drain(self.guard)
        self.guard.full_pass()
        self.guard.full_pass()
        opts = self.db.options("lrp-" + PORT)
        self.assertNotIn(RA.OPTION, opts)
        self.assertEqual(opts[RA.MARKER], "wanted")
        self.assertEqual(self.logs.text.count("is marked but has no"), 1)

    def test_rate_limit_suspends_and_reports(self):
        now = [1000.0]
        db = site()
        svc, store, guard = service(db, clock=lambda: now[0])
        put(svc, PORT, True)
        drain(guard)
        for i in range(5):
            db.external_write("lrp-" + PORT, RA.OPTION, "connected-as-host")
            now[0] += 10
            drain(guard)
            self.assertIn("nat", db.options("lrp-" + PORT)[RA.OPTION], i)
        db.external_write("lrp-" + PORT, RA.OPTION, "connected-as-host")
        drain(guard)
        self.assertEqual(db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host", "suspended: no 6th restore")
        health = svc.handle("GET", "/healthz", {}, None)[1]["guard"]
        self.assertEqual(health, {"wanted": 1, "restoredSinceStart": 5, "suspended": [PORT]})
        self.assertEqual(self.logs.text.count("the guard stops restoring"), 1)
        now[0] += 1000
        guard.full_pass()
        self.assertEqual(db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host", "stays suspended")
        # the platform asks again: the suspension is lifted
        put(svc, PORT, True)
        self.assertEqual(svc.handle("GET", "/healthz", {}, None)[1]["guard"]["suspended"], [])
        self.assertEqual(db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host,nat")

    def test_restores_spread_over_time_are_not_suspended(self):
        now = [1000.0]
        db = site()
        svc, store, guard = service(db, clock=lambda: now[0])
        put(svc, PORT, True)
        drain(guard)
        for _ in range(8):
            db.external_write("lrp-" + PORT, RA.OPTION, "connected-as-host")
            now[0] += 100
            drain(guard)
        self.assertEqual(db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host,nat")
        self.assertEqual(guard.summary()["suspended"], [])

    def test_restart_restores_from_the_marker_alone(self):
        put(self.svc, PORT, True)
        self.db.listeners.clear()  # the old service is gone
        self.db.external_write("lrp-" + PORT, RA.OPTION, "connected-as-host")
        svc2, store2, guard2 = service(self.db)
        drain(guard2)
        self.assertEqual(self.db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host,nat")
        self.assertEqual(svc2.handle("GET", "/healthz", {}, None)[1]["guard"]["wanted"], 1)

    def test_unmarked_ports_are_never_touched(self):
        versions = dict(self.db.versions)
        drain(self.guard)
        self.guard.full_pass()
        for name in list(self.db.rows):
            self.db.external_write(name, "unrelated", "1")
        drain(self.guard)
        self.assertEqual(self.db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host")
        self.assertEqual(self.db.options("lrp-" + PORT2)[RA.OPTION], "static, connected-as-host,static")
        self.assertEqual({n: v + 1 for n, v in versions.items()}, self.db.versions, "only the external writes")
        self.assertNotIn("guard: restored", self.logs.text)

    def test_disabled_port_is_not_restored(self):
        put(self.svc, PORT, True)
        put(self.svc, PORT, False)
        drain(self.guard)
        self.guard.full_pass()
        self.assertEqual(self.db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host")
        self.assertEqual(self.guard.summary()["wanted"], 0)

    def test_guard_and_concurrent_api_disable_do_not_lose_an_update(self):
        """The guard reads a marked port without nat; before its commit the API disables the port: the guard's
        transaction is re-run, finds no marker, and writes nothing."""
        lrp = "lrp-" + PORT
        put(self.svc, PORT, True)
        drain(self.guard)
        self.db.listeners.clear()
        self.db.external_write(lrp, RA.OPTION, "connected-as-host")
        self.db.before_commit = lambda: put(self.svc, PORT, False)
        result = self.store.restore_nat(lrp)
        self.assertEqual(result["outcome"], "unmarked")
        opts = self.db.options(lrp)
        self.assertEqual(opts[RA.OPTION], "connected-as-host")
        self.assertNotIn(RA.MARKER, opts)

    def test_guard_and_concurrent_neutron_write_do_not_lose_an_update(self):
        lrp = "lrp-" + PORT
        put(self.svc, PORT, True)
        drain(self.guard)
        self.db.listeners.clear()
        self.db.external_write(lrp, RA.OPTION, "connected-as-host")
        self.db.before_commit = lambda: self.db.external_write(lrp, RA.OPTION, "static,connected-as-host")
        self.guard.handle_event(lrp)
        self.assertEqual(self.db.options(lrp)[RA.OPTION], "static,connected-as-host,nat")

    def test_guard_thread_end_to_end(self):
        db = site()
        svc, store, guard = service(db)
        guard.start()
        self.addCleanup(guard.stop)
        put(svc, PORT, True)
        db.external_write("lrp-" + PORT, RA.OPTION, "connected-as-host")
        deadline = time.time() + 5
        while time.time() < deadline and "nat" not in db.options("lrp-" + PORT)[RA.OPTION]:
            time.sleep(0.02)
        self.assertEqual(db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host,nat")

    def test_notify_never_blocks(self):
        guard = RA.Guard(FakePortStore(site()))
        for _ in range(3000):
            guard.notify("lrp-" + PORT)
        self.assertTrue(guard._rescan.is_set(), "an overflow asks for a full pass")

    def test_healthz_shows_ids_only(self):
        now = [1000.0]
        db = site()
        svc, store, guard = service(db, clock=lambda: now[0])
        put(svc, PORT, True)
        for _ in range(6):
            db.external_write("lrp-" + PORT, RA.OPTION, "connected-as-host")
            drain(guard)
        body = svc.handle("GET", "/healthz", {}, None)[1]
        self.assertEqual(set(body), {"ok", "ovn", "guard"})
        self.assertEqual(body["guard"]["suspended"], [PORT])
        text = json.dumps(body)
        for word in ("lrp-", PROJECT, USER, "connected-as-host"):
            self.assertNotIn(word, text)


# ---------------------------------------------------------------------------------------------------------------
# Keystone and Neutron calls

class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def getcode(self):
        return self.status

    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]

    def close(self):
        pass


class FakeOpener:
    def __init__(self, answers):
        self.answers = answers
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request.full_url, dict(request.header_items()), timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        if status >= 300:
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(b"{}"))
        return FakeResponse(status, body)


class OpenStackClientTest(unittest.TestCase):
    def client(self, answers):
        opener = FakeOpener(answers)
        return RA.OpenStackClient("https://keystone.example.com:5000", "https://neutron.example.com:9696",
                                  timeout=7, opener=opener), opener

    def test_validate_sends_the_token_as_both_headers_with_a_timeout(self):
        c, o = self.client([(200, {"token": {"user": {"id": USER}, "project": {"id": PROJECT}}})])
        self.assertEqual(c.validate(TOKEN), {"user_id": USER, "project_id": PROJECT})
        url, headers, timeout = o.requests[0]
        self.assertEqual(url, "https://keystone.example.com:5000/v3/auth/tokens?nocatalog")
        self.assertEqual(headers["X-auth-token"], TOKEN)
        self.assertEqual(headers["X-subject-token"], TOKEN)
        self.assertEqual(timeout, 7)

    def test_validate_unscoped(self):
        c, _ = self.client([(200, {"token": {"user": {"id": USER}, "domain": {"id": "default"}}})])
        self.assertIsNone(c.validate(TOKEN)["project_id"])

    def test_validate_refusals_and_failures(self):
        for answer, reason in (((404, {}), "Unauthenticated"), ((401, {}), "Unauthenticated"),
                               ((500, {}), "IdentityUnavailable"), ((302, {}), "IdentityUnavailable"),
                               ((200, b"<html>"), "IdentityUnavailable"), ((200, {"x": 1}), "IdentityUnavailable"),
                               (urllib.error.URLError("refused"), "IdentityUnavailable"),
                               (socket.timeout("timed out"), "IdentityUnavailable"),
                               (ConnectionResetError(), "IdentityUnavailable")):
            c, _ = self.client([answer])
            with self.assertRaises(RA.ServiceError) as cm:
                c.validate(TOKEN)
            self.assertEqual(cm.exception.reason, reason, answer)

    def test_port(self):
        port = {"id": PORT, "project_id": PROJECT, "device_owner": "network:router_interface"}
        c, o = self.client([(200, {"port": port})])
        self.assertEqual(c.port(TOKEN, PORT), port)
        self.assertEqual(o.requests[0][0], "https://neutron.example.com:9696/v2.0/ports/" + PORT)
        self.assertNotIn("X-subject-token", o.requests[0][1])
        for answer, expect in (((404, {}), None), ((403, {}), None)):
            c, _ = self.client([answer])
            self.assertIsNone(c.port(TOKEN, PORT))
        for answer, reason in (((401, {}), "Unauthenticated"), ((503, {}), "NetworkUnavailable"),
                               (socket.timeout(), "NetworkUnavailable"), ((200, {"ports": []}), "NetworkUnavailable")):
            c, _ = self.client([answer])
            with self.assertRaises(RA.ServiceError) as cm:
                c.port(TOKEN, PORT)
            self.assertEqual(cm.exception.reason, reason)

    def test_redirects_are_refused(self):
        handler = RA._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example.com/"))

    def test_failures_do_not_log_the_token(self):
        with LogCapture() as logs:
            c, _ = self.client([urllib.error.URLError("refused")])
            with self.assertRaises(RA.ServiceError):
                c.validate(TOKEN)
        self.assertIn("IdentityUnavailable", logs.text)
        self.assertNotIn(TOKEN, logs.text)


# ---------------------------------------------------------------------------------------------------------------
# Settings

class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.files = {}
        for name in ("key", "cert", "ca"):
            path = os.path.join(self.tmp.name, name)
            with open(path, "w") as fh:
                fh.write("x")
            self.files[name] = path

    def env(self, **over):
        env = {"OVN_NB_CONNECTION": "ssl:ovsdb-nb.example.com:6641", "OVN_NB_PRIVATE_KEY": self.files["key"],
               "OVN_NB_CERTIFICATE": self.files["cert"], "OVN_NB_CA_CERT": self.files["ca"],
               "KEYSTONE_URL": "https://keystone.example.com:5000/v3/",
               "NEUTRON_URL": "https://neutron.example.com:9696"}
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def test_defaults(self):
        s = RA.load_settings(self.env())
        self.assertEqual((s.listen_address, s.listen_port, s.ovn_timeout, s.api_timeout, s.guard_interval),
                         ("0.0.0.0", 8080, 10.0, 10.0, 60.0))
        self.assertEqual(s.keystone_url, "https://keystone.example.com:5000")
        self.assertTrue(s.ovn_ssl)
        self.assertIsNone(s.tls_certificate)

    def test_each_required_setting_is_named_when_missing(self):
        for name in ("OVN_NB_CONNECTION", "KEYSTONE_URL", "NEUTRON_URL", "OVN_NB_PRIVATE_KEY",
                     "OVN_NB_CERTIFICATE", "OVN_NB_CA_CERT"):
            with self.assertRaises(RA.ConfigError) as cm:
                RA.load_settings(self.env(**{name: None}))
            self.assertTrue(str(cm.exception).startswith(name + ":"), str(cm.exception))

    def test_tcp_needs_no_certificates(self):
        s = RA.load_settings(self.env(OVN_NB_CONNECTION="tcp:192.0.2.10:6641,tcp:192.0.2.11:6641",
                                      OVN_NB_PRIVATE_KEY=None, OVN_NB_CERTIFICATE=None, OVN_NB_CA_CERT=None))
        self.assertFalse(s.ovn_ssl)

    def test_invalid_values_are_named(self):
        cases = {"OVN_NB_CONNECTION": "unix:/run/nb.sock", "KEYSTONE_URL": "ftp://x", "NEUTRON_URL": "neutron",
                 "LISTEN_PORT": "0", "OVN_TIMEOUT_SECONDS": "soon", "GUARD_INTERVAL_SECONDS": "1",
                 "OS_CACERT": "/nonexistent/ca.pem", "OVN_NB_CA_CERT": "/nonexistent/ca.pem", "LOG_LEVEL": "LOUD"}
        for name, value in cases.items():
            with self.assertRaises(RA.ConfigError) as cm:
                RA.load_settings(self.env(**{name: value}))
            self.assertTrue(str(cm.exception).startswith(name + ":"), (name, str(cm.exception)))

    def test_tls_needs_both_files(self):
        with self.assertRaises(RA.ConfigError) as cm:
            RA.load_settings(self.env(TLS_CERTIFICATE=self.files["cert"]))
        self.assertTrue(str(cm.exception).startswith("TLS_PRIVATE_KEY:"))
        with self.assertRaises(RA.ConfigError) as cm:
            RA.load_settings(self.env(TLS_PRIVATE_KEY=self.files["key"]))
        self.assertTrue(str(cm.exception).startswith("TLS_CERTIFICATE:"))

    def test_main_refuses_to_start_with_the_settings_name(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        with LogCapture() as logs:
            rc = RA.main(env=self.env(KEYSTONE_URL=None))
        root.handlers[:] = handlers
        root.setLevel(level)
        self.assertEqual(rc, 2)
        self.assertIn("refusing to start: KEYSTONE_URL: required setting is missing", logs.text)


# ---------------------------------------------------------------------------------------------------------------
# HTTP on the loopback

class HttpTest(Base):
    def setUp(self):
        super().setUp()
        self.server = RA.build_server(self.svc, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, token=TOKEN, timeout=10):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Auth-Token"] = token
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data, resp

    def test_put_and_get_over_http(self):
        status, body, resp = self.call("PUT", "/v1/router-ports/%s/nat" % PORT, b'{"enabled": true}')
        self.assertEqual((status, body["changed"]), (200, True))
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        status, body, _ = self.call("GET", "/v1/router-ports/%s" % PORT)
        self.assertTrue(body["nat"])
        status, body, _ = self.call("GET", "/healthz", token=None)
        self.assertEqual((status, body["ovn"]), (200, "connected"))

    def test_put_without_length_or_with_a_huge_one(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("PUT", "/v1/router-ports/%s/nat" % PORT)
        conn.putheader("X-Auth-Token", TOKEN)
        conn.putheader("Content-Length", "999999")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_a_body_that_is_not_read_closes_the_connection(self):
        # Review R2 L2: the leftover bytes of a POST (or a GET with a body) are never parsed as the next request
        smuggled = b"GET /v1/router-ports/%s HTTP/1.1\r\nHost: x\r\nX-Auth-Token: %s\r\n\r\n" % (PORT.encode(), TOKEN.encode())
        for method in (b"POST", b"GET", b"DELETE"):
            with self.subTest(method=method):
                s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                s.sendall(method + b" /v1/router-ports/" + PORT.encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Length: "
                          + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled)
                data = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                s.close()
                self.assertEqual(data.count(b"HTTP/1.1 "), 1, data)   # one answer, then the connection closed
                self.assertIn(b"Connection: close", data)
        # chunked bodies are refused for PUT and close the connection
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(b"PUT /v1/router-ports/" + PORT.encode() + b"/nat HTTP/1.1\r\nHost: x\r\nX-Auth-Token: " + TOKEN.encode()
                  + b"\r\nTransfer-Encoding: chunked\r\n\r\n11\r\n{\"enabled\": true}\r\n0\r\n\r\n")
        data = s.recv(65536)
        s.close()
        self.assertIn(b"400", data.split(b"\r\n")[0])
        self.assertNotIn("hybrid-fabric-nat", self.db.options("lrp-" + PORT))

    def test_concurrent_http_requests(self):
        results = []

        def worker(i):
            port_id = PORT if i % 2 else PORT2
            try:
                results.append(self.call("PUT", "/v1/router-ports/%s/nat" % port_id, b'{"enabled": true}')[0])
            except Exception as exc:
                results.append(describe_exc(exc))

        self.assertGreaterEqual(self.server.request_queue_size, 64)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [200] * 60)
        self.assertEqual(self.db.options("lrp-" + PORT)[RA.OPTION], "connected-as-host,nat")

    def test_a_slow_dependency_does_not_block_other_requests(self):
        self.svc.openstack.delay = 1.5
        slow = threading.Thread(target=self.call, args=("GET", "/v1/router-ports/%s" % PORT))
        slow.start()
        time.sleep(0.1)
        started = time.monotonic()
        status, _, _ = self.call("GET", "/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 1.0)
        slow.join()

    def test_graceful_shutdown_finishes_requests_in_flight(self):
        self.svc.openstack.delay = 0.8
        out = []
        slow = threading.Thread(target=lambda: out.append(self.call("GET", "/v1/router-ports/%s" % PORT)[0]))
        slow.start()
        time.sleep(0.2)
        self.server.shutdown()
        self.server.server_close()
        slow.join()
        self.assertEqual(out, [200])

    def test_no_token_in_logs_over_http(self):
        self.call("PUT", "/v1/router-ports/%s/nat" % PORT, b'{"enabled": true}')
        self.call("GET", "/v1/router-ports/%s" % FOREIGN)
        self.call("GET", "/v1/router-ports/%s" % PORT, token="gAAAAABnotKnownToKeystone")
        self.assertIn("PUT /v1/router-ports/%s/nat 200" % PORT, self.logs.text)
        self.assertNotIn("gAAAAABnotKnownToKeystone", self.logs.text)


# ---------------------------------------------------------------------------------------------------------------
# The ovsdbapp store, with stand-in modules

class StandInModules:
    """Just enough of ovs / ovsdbapp for the store's wiring: schema helper, IDL, Connection, NB API, BaseCommand."""

    def __init__(self):
        self.calls = []
        self.rows = {}
        self.connected = True
        self.fail_commit = None
        calls, owner = self.calls, self

        class Stream:
            @staticmethod
            def ssl_set_private_key_file(p):
                calls.append(("key", p))

            @staticmethod
            def ssl_set_certificate_file(p):
                calls.append(("cert", p))

            @staticmethod
            def ssl_set_ca_cert_file(p):
                calls.append(("ca", p))

        class Helper:
            def register_table(self, t):
                calls.append(("register_table", t))

        class Table:
            @property
            def rows(self):
                return owner.rows

        class OvsdbIdl:
            def __init__(self, remote, helper, leader_only=False):
                calls.append(("idl", remote, leader_only))
                self.tables = {"Logical_Router_Port": Table()}
                owner.idl = self

            def is_connected(self):
                return owner.connected

        class Connection:
            def __init__(self, idl, timeout):
                calls.append(("connection", timeout))
                self.idl = idl

            def stop(self, timeout=None):
                calls.append(("stop",))

        class Api:
            def __init__(self, conn):
                self.ovsdb_connection = conn
                self.idl = conn.idl

        class BaseCommand:
            def __init__(self, api):
                self.api = api
                self.result = None

            def execute(self, **kw):
                calls.append(("execute", kw))
                if owner.fail_commit:
                    raise owner.fail_commit
                self.run_idl(None)
                return self.result

        def get_schema_helper(remote, schema):
            calls.append(("schema", remote, schema))
            return Helper()

        def mod(name, **attrs):
            m = types.ModuleType(name)
            m.__dict__.update(attrs)
            return m

        self.modules = {
            "ovs": mod("ovs"), "ovs.db": mod("ovs.db"), "ovs.db.idl": mod("ovs.db.idl", Idl=OvsdbIdl),
            "ovs.stream": mod("ovs.stream", Stream=Stream),
            "ovsdbapp": mod("ovsdbapp"), "ovsdbapp.backend": mod("ovsdbapp.backend"),
            "ovsdbapp.backend.ovs_idl": mod("ovsdbapp.backend.ovs_idl"),
            "ovsdbapp.backend.ovs_idl.connection": mod("c", OvsdbIdl=OvsdbIdl, Connection=Connection),
            "ovsdbapp.backend.ovs_idl.idlutils": mod("i", get_schema_helper=get_schema_helper),
            "ovsdbapp.backend.ovs_idl.command": mod("cmd", BaseCommand=BaseCommand),
            "ovsdbapp.schema": mod("ovsdbapp.schema"), "ovsdbapp.schema.ovn_northbound": mod("n"),
            "ovsdbapp.schema.ovn_northbound.impl_idl": mod("ni", OvnNbApiIdlImpl=Api),
        }
        # package attributes so `from x import y` finds the submodules
        self.modules["ovs"].db = self.modules["ovs.db"]
        self.modules["ovs"].stream = self.modules["ovs.stream"]
        self.modules["ovs.db"].idl = self.modules["ovs.db.idl"]
        self.modules["ovsdbapp"].backend = self.modules["ovsdbapp.backend"]
        self.modules["ovsdbapp.backend"].ovs_idl = self.modules["ovsdbapp.backend.ovs_idl"]
        b = self.modules["ovsdbapp.backend.ovs_idl"]
        b.connection = self.modules["ovsdbapp.backend.ovs_idl.connection"]
        b.idlutils = self.modules["ovsdbapp.backend.ovs_idl.idlutils"]
        b.command = self.modules["ovsdbapp.backend.ovs_idl.command"]
        self.modules["ovsdbapp"].schema = self.modules["ovsdbapp.schema"]
        self.modules["ovsdbapp.schema"].ovn_northbound = self.modules["ovsdbapp.schema.ovn_northbound"]
        self.modules["ovsdbapp.schema.ovn_northbound"].impl_idl = self.modules["ovsdbapp.schema.ovn_northbound.impl_idl"]


class OvsdbappStoreTest(unittest.TestCase):
    def setUp(self):
        self.fake = StandInModules()
        patcher = unittest.mock.patch.dict(sys.modules, self.fake.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.unlink(os.path.join(tmp, f)) for f in os.listdir(tmp)] and os.rmdir(tmp))
        paths = {}
        for n in ("k", "c", "a"):
            paths[n] = os.path.join(tmp, n)
            open(paths[n], "w").close()
        self.settings = RA.load_settings({
            "OVN_NB_CONNECTION": "ssl:ovsdb-nb.example.com:6641", "OVN_NB_PRIVATE_KEY": paths["k"],
            "OVN_NB_CERTIFICATE": paths["c"], "OVN_NB_CA_CERT": paths["a"],
            "KEYSTONE_URL": "https://keystone.example.com:5000", "NEUTRON_URL": "https://neutron.example.com:9696",
            "OVN_TIMEOUT_SECONDS": "7"})
        self.events = []
        self.store = RA.OvsdbappPortStore(self.settings)
        self.store.start(on_change=self.events.append)
        self.store._thread.join(5)
        self.addCleanup(self.store.stop)

    def test_wiring(self):
        calls = self.fake.calls
        self.assertIn(("key", self.settings.ovn_private_key), calls)
        self.assertIn(("ca", self.settings.ovn_ca_cert), calls)
        self.assertIn(("schema", "ssl:ovsdb-nb.example.com:6641", "OVN_Northbound"), calls)
        self.assertIn(("register_table", "Logical_Router_Port"), calls)
        self.assertIn(("idl", "ssl:ovsdb-nb.example.com:6641", True), calls)
        self.assertIn(("connection", 7.0), calls)
        self.assertTrue(self.store.connected())

    def test_row_changes_reach_the_guard_callback(self):
        row = types.SimpleNamespace(name="lrp-" + PORT)
        self.fake.idl.notify("update", row, None)
        self.assertEqual(self.events, ["lrp-" + PORT])

    def test_commands_run_the_steps_and_map_failures(self):
        self.fake.rows = {1: FakeRow("lrp-" + PORT, {RA.OPTION: "connected-as-host"}, 1)}
        self.assertEqual(self.store.read_port("lrp-" + PORT), {"redistribute": ["connected-as-host"],
                                                              "wanted": False})
        result = self.store.set_nat("lrp-" + PORT, True)
        self.assertTrue(result["changed"])
        self.assertTrue(self.fake.rows[1].verified)
        self.assertIn(("execute", {"check_error": True, "log_errors": False}), self.fake.calls)
        with self.assertRaises(RA.ServiceError) as cm:
            self.store.read_port("lrp-" + NO_LRP)
        self.assertEqual(cm.exception.reason, "NoSuchRouterPort")
        self.fake.fail_commit = RuntimeError("OVSDB Error: timeout")
        with self.assertRaises(RA.ServiceError) as cm:
            self.store.set_nat("lrp-" + PORT, True)
        self.assertEqual(cm.exception.reason, "OvnUnavailable")
        self.fake.fail_commit = None
        self.fake.connected = False
        with self.assertRaises(RA.ServiceError) as cm:
            self.store.marked_ports()
        self.assertEqual(cm.exception.reason, "OvnUnavailable")


# ---------------------------------------------------------------------------------------------------------------
# The deployment assets

COMPONENT = os.path.join(ROOT, "site-components/router-advertisement")
TEMPLATE = os.path.join(COMPONENT, "deploy/rhoso/router-advertisement.yaml")
LAB = ("signal9", "172.18.", "75.8.222.", "2602:81f", "atuin", "rhoso-bm", "shc-", "xna-", "quay.")


class DeployAssetsTest(unittest.TestCase):
    def setUp(self):
        try:
            import yaml
        except ImportError:  # pragma: no cover
            self.skipTest("PyYAML is not installed")
        with open(TEMPLATE) as fh:
            self.text = fh.read()
        self.template = yaml.safe_load(self.text)
        self.objects = {o["kind"]: o for o in self.template["objects"]}

    def test_every_deployment_name_is_a_parameter(self):
        params = {p["name"]: p for p in self.template["parameters"]}
        for name in ("NAMESPACE", "IMAGE", "OVN_CA_ISSUER", "ROUTE_HOST", "OVN_NB_CONNECTION", "KEYSTONE_URL",
                     "NEUTRON_URL"):
            self.assertIn(name, params)
        for name in ("NAMESPACE", "IMAGE", "OVN_CA_ISSUER", "OVN_NB_CONNECTION", "KEYSTONE_URL", "NEUTRON_URL"):
            self.assertTrue(params[name].get("required"), name)
            self.assertNotIn("value", params[name], name)
        for obj in self.template["objects"]:
            self.assertEqual(obj["metadata"]["namespace"], "${NAMESPACE}")
        pod = self.objects["Deployment"]["spec"]["template"]["spec"]
        self.assertEqual(pod["containers"][0]["image"], "${IMAGE}")
        self.assertEqual(self.objects["Certificate"]["spec"]["issuerRef"]["name"], "${OVN_CA_ISSUER}")
        self.assertEqual(self.objects["Route"]["spec"]["host"], "${ROUTE_HOST}")

    def test_pod_hardening(self):
        self.assertFalse(self.objects["ServiceAccount"]["automountServiceAccountToken"])
        pod = self.objects["Deployment"]["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        c = pod["containers"][0]
        self.assertTrue(c["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(c["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(c["securityContext"]["capabilities"]["drop"], ["ALL"])
        for kind in ("requests", "limits"):
            self.assertEqual(set(c["resources"][kind]), {"cpu", "memory"})
        self.assertEqual(c["livenessProbe"]["httpGet"]["path"], "/healthz")
        self.assertNotIn("Role", self.objects)
        self.assertNotIn("RoleBinding", self.objects)

    def test_the_settings_the_program_requires_are_set(self):
        c = self.objects["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"] for e in c["env"]}
        for name in ("OVN_NB_CONNECTION", "OVN_NB_PRIVATE_KEY", "OVN_NB_CERTIFICATE", "OVN_NB_CA_CERT",
                     "KEYSTONE_URL", "NEUTRON_URL", "TLS_CERTIFICATE", "TLS_PRIVATE_KEY"):
            self.assertIn(name, env)
        self.assertEqual(c["command"][-1], "/opt/router-advertisement/router_advertisement.py")

    def test_no_lab_values(self):
        paths = [TEMPLATE, PATH, os.path.join(COMPONENT, "deploy/rhoso/README.md"),
                 os.path.join(COMPONENT, "deploy/container/README.md"),
                 os.path.join(ROOT, "docs/platform/router-advertisement-service.md")]
        for path in paths:
            with open(path) as fh:
                text = fh.read().lower()
            for word in LAB:
                self.assertNotIn(word, text, (path, word))
            # the service is not registered in Keystone: the realization names its URL per site
            for word in ("service create", "endpoint create", "hybrid-fabric-ovn", "catalog"):
                self.assertNotIn(word, text.replace("nocatalog", ""), (path, word))


import unittest.mock  # noqa: E402  (used by OvsdbappStoreTest)

if __name__ == "__main__":
    unittest.main()
