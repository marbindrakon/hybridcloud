#!/usr/bin/env python3
"""Router advertisement service: the `nat` token of an OVN router port, on behalf of the port's own project.

A floating IP behind a Neutron router whose interface is attached to a network with EVPN host advertisement is
advertised by OVN only when the router port carries `nat` in `options:dynamic-routing-redistribute`. Neutron's EVPN
plugin writes that option without `nat`, and the option cannot be reached through the Neutron API. This service does
one thing: it adds or removes the `nat` token on the OVN Logical_Router_Port `lrp-<port id>` of a Neutron router
interface port, for a caller whose Keystone token is scoped to the port's project, and keeps the token in place
between calls (the guard).

It runs inside the site's own Neutron server image (Python 3.9 or later, the standard library, `ovsdbapp` and the OVS
Python bindings). It holds no OpenStack credential: every call is authorized by the caller's own token.

API (JSON):
  GET  /healthz                              no authentication; always 200 with the state
  GET  /readyz                               no authentication; 200 while the database is connected, else 503
With PROBE_PORT set, /healthz and /readyz are also served in plain HTTP on that port, by a listener of their own (a
few connections, a short deadline) that serves nothing else: the kubelet's probes never wait for a slot of the
service's listener, whoever holds those.
  GET  /v1/router-ports/{port_id}            header X-Auth-Token
  PUT  /v1/router-ports/{port_id}/nat        header X-Auth-Token, body {"enabled": true|false}

The wish is stored on the port itself (`options:hybrid-fabric-nat=wanted`, written in the same transaction as the
token); the service keeps no state of its own beyond counters since its start.

Configuration (environment): OVN_NB_CONNECTION, OVN_NB_PRIVATE_KEY, OVN_NB_CERTIFICATE, OVN_NB_CA_CERT (the last
three for ssl: connections), KEYSTONE_URL, NEUTRON_URL, OS_CACERT, LISTEN_ADDRESS, LISTEN_PORT, TLS_CERTIFICATE,
TLS_PRIVATE_KEY, OVN_TIMEOUT_SECONDS, API_TIMEOUT_SECONDS, GUARD_INTERVAL_SECONDS, MAX_CONNECTIONS,
CONNECTION_DEADLINE_SECONDS, PROBE_PORT, LOG_LEVEL.
"""
import collections
import datetime
import http.client
import http.server
import json
import logging
import os
import queue
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("router-advertisement")

OPTION = "dynamic-routing-redistribute"
MARKER = "hybrid-fabric-nat"
MARKER_VALUE = "wanted"
NAT = "nat"
LRP_TABLE = "Logical_Router_Port"
NB_SCHEMA = "OVN_Northbound"
LRP_PREFIX = "lrp-"
# Neutron's router interface owners (neutron_lib ROUTER_INTERFACE_OWNERS).
ROUTER_INTERFACE_OWNERS = ("network:router_interface", "network:router_interface_distributed",
                           "network:ha_router_replicated_interface")

PORT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Keystone tokens (Fernet, JWS) are URL-safe base64 with dots; anything else is not a token.
TOKEN = re.compile(r"^[A-Za-z0-9_\-.=+/~]{1,8192}$")
OPTION_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ROUTES = (
    ("port", re.compile(r"^/v1/router-ports/([^/]+)$")),
    ("nat", re.compile(r"^/v1/router-ports/([^/]+)/nat$")),
)
MAX_BODY = 4096
# Connections served at once, and the longest a connection may stay open (seconds); settings MAX_CONNECTIONS and
# CONNECTION_DEADLINE_SECONDS.
MAX_CONNECTIONS = 32
CONNECTION_DEADLINE = 60.0
# the probe listener (PROBE_PORT): connections at once and their deadline
PROBE_CONNECTIONS = 4
PROBE_DEADLINE = 10.0
PROBE_PATHS = ("/healthz", "/readyz")
MAX_RESPONSE = 1 << 20

# Guard rate limit: at most RESTORE_LIMIT restores of one port within RESTORE_WINDOW seconds.
RESTORE_LIMIT = 5
RESTORE_WINDOW = 300.0


# ---------------------------------------------------------------------------------------------------------------
# Errors

class ServiceError(Exception):
    """A refusal or failure that the API returns as {"error": reason, "message": message}."""

    def __init__(self, status, reason, message):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message


def bad_request(message):
    return ServiceError(400, "BadRequest", message)


def unauthenticated(message="The request needs a valid Keystone token in X-Auth-Token."):
    return ServiceError(401, "Unauthenticated", message)


def ovn_unavailable(message="The OVN northbound database did not answer."):
    return ServiceError(502, "OvnUnavailable", message)


def identity_unavailable(message="The identity service did not answer."):
    return ServiceError(502, "IdentityUnavailable", message)


def network_unavailable(message="The networking service did not answer."):
    return ServiceError(502, "NetworkUnavailable", message)


def no_such_router_port(lrp):
    return ServiceError(404, "NoSuchRouterPort", "OVN has no logical router port %s." % lrp)


def not_advertising(lrp):
    return ServiceError(409, "NotAnAdvertisingPort",
                        "The router port %s has no %s option: its network is not attached with host advertisement, "
                        "and the service does not create the option." % (lrp, OPTION))


class ConfigError(Exception):
    """A setting is missing or invalid; the message starts with the setting's name."""


# ---------------------------------------------------------------------------------------------------------------
# The option's token list

def parse_tokens(value):
    """The tokens of a redistribute option value: comma-separated, order kept, blanks and duplicates dropped."""
    out = []
    for part in (value or "").split(","):
        token = part.strip()
        if token and token not in out:
            out.append(token)
    return out


def serialise_tokens(tokens):
    return ",".join(tokens)


def with_nat(tokens, enabled):
    """The token list with `nat` appended (when absent) or removed; every other token and its order kept."""
    tokens = [t for t in tokens if t != NAT]
    if enabled:
        tokens.append(NAT)
    return tokens


def lrp_name(port_id):
    return LRP_PREFIX + port_id


def port_id_of(lrp):
    return lrp[len(LRP_PREFIX):] if lrp.startswith(LRP_PREFIX) else lrp


# ---------------------------------------------------------------------------------------------------------------
# Transaction steps. They run inside one OVSDB transaction on a Logical_Router_Port row (an ovs.db.idl.Row, or a
# fake with the same .options / verify / setkey / delkey). verify("options") makes the commit fail with TRY_AGAIN
# when anyone changed the options column after it was read; the transaction is then re-run from the read
# (ovsdbapp re-runs every command of a transaction on TRY_AGAIN), so a concurrent rewrite is never overwritten.

def find_row(rows, name):
    for row in rows:
        if getattr(row, "name", None) == name:
            return row
    return None


def port_state(row):
    """What the API and the guard read from a row: the token list (None without the option) and the marker."""
    options = dict(getattr(row, "options", None) or {})
    raw = options.get(OPTION)
    return {"redistribute": None if raw is None else parse_tokens(raw),
            "wanted": options.get(MARKER) == MARKER_VALUE}


def step_set_nat(row, lrp, enabled):
    """The API's write: the token and the marker together. Refuses a port without the option."""
    if row is None:
        raise no_such_router_port(lrp)
    options = dict(row.options or {})
    if OPTION not in options:
        raise not_advertising(lrp)
    row.verify("options")
    before = parse_tokens(options[OPTION])
    after = with_nat(before, enabled)
    marked = options.get(MARKER) == MARKER_VALUE
    token_changed = (NAT in before) != enabled
    if token_changed:
        row.setkey("options", OPTION, serialise_tokens(after))
    else:
        after = list(before)
    marker_changed = marked != enabled
    if marker_changed:
        if enabled:
            row.setkey("options", MARKER, MARKER_VALUE)
        else:
            row.delkey("options", MARKER)
    return {"before": before, "after": after, "changed": token_changed or marker_changed}


def step_restore(row, lrp):
    """The guard's write: append `nat` only on a marked port whose option exists and lacks it."""
    if row is None:
        return {"outcome": "gone", "before": None, "after": None, "changed": False}
    state = port_state(row)
    if not state["wanted"]:
        return {"outcome": "unmarked", "before": state["redistribute"], "after": state["redistribute"],
                "changed": False}
    if state["redistribute"] is None:
        return {"outcome": "no-option", "before": None, "after": None, "changed": False}
    before = state["redistribute"]
    if NAT in before:
        return {"outcome": "present", "before": before, "after": before, "changed": False}
    row.verify("options")
    after = with_nat(before, True)
    row.setkey("options", OPTION, serialise_tokens(after))
    return {"outcome": "restored", "before": before, "after": after, "changed": True}


def scan_marked(rows):
    """Every marked row: {lrp name: port_state}."""
    out = {}
    for row in rows:
        state = port_state(row)
        if state["wanted"]:
            out[getattr(row, "name", "")] = state
    return out


# ---------------------------------------------------------------------------------------------------------------
# OVN access: one connection, every read and write a command on its transaction queue.

class OvsdbappPortStore:
    """The OVN northbound database through ovsdbapp.

    One ovsdbapp Connection: its own thread owns the IDL replica, runs every transaction in turn and reconnects
    (the OVS IDL's session retries with backoff). Request threads and the guard thread submit commands to it and
    wait at most OVN_TIMEOUT_SECONDS. Row change notifications arrive on that thread and are only passed on
    (a callback that must not block or transact)."""

    def __init__(self, settings):
        self._settings = settings
        self._lock = threading.Lock()
        self._api = None
        self._idl = None
        self._stop = threading.Event()
        self._thread = None
        self._on_change = None
        self._reported = set()

    def _report_once(self, message):
        """An unexpected failure on a path that must not raise: logged once per distinct message."""
        if message not in self._reported:
            self._reported.add(message)
            LOG.error("OVN connection: %s", message)

    # lifecycle -------------------------------------------------------------------------------------------------
    def start(self, on_change=None):
        self._on_change = on_change
        self._thread = threading.Thread(target=self._connect_loop, name="ovn-connect", daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        with self._lock:
            api = self._api
            self._api = None
        if api is not None:
            try:
                api.ovsdb_connection.stop(timeout=timeout)
            except Exception as exc:  # shutting down: nothing more to do
                LOG.debug("OVN connection stop: %s", type(exc).__name__)

    def _connect_loop(self):
        delay = 1.0
        while not self._stop.is_set():
            try:
                api, idl = self._connect()
            except Exception as exc:
                LOG.warning("OVN northbound database not reachable (%s); retrying in %.0f s", describe(exc), delay)
                self._stop.wait(delay)
                delay = min(delay * 2, 30.0)
                continue
            with self._lock:
                self._api, self._idl = api, idl
            LOG.info("connected to the OVN northbound database")
            return

    def _connect(self):
        from ovs.db import idl as ovs_idl
        from ovs.stream import Stream
        from ovsdbapp.backend.ovs_idl import connection
        from ovsdbapp.backend.ovs_idl import idlutils
        from ovsdbapp.schema.ovn_northbound import impl_idl

        s = self._settings
        if s.ovn_ssl:
            Stream.ssl_set_private_key_file(s.ovn_private_key)
            Stream.ssl_set_certificate_file(s.ovn_certificate)
            Stream.ssl_set_ca_cert_file(s.ovn_ca_cert)
        helper = idlutils.get_schema_helper(s.ovn_connection, NB_SCHEMA)
        helper.register_table(LRP_TABLE)
        base = getattr(connection, "OvsdbIdl", None) or ovs_idl.Idl
        on_change = self._on_change
        report = self._report_once

        class _Idl(base):
            def notify(self, event, row, *args, **kwargs):
                if on_change is not None:
                    try:
                        on_change(getattr(row, "name", None))
                    except Exception as exc:  # never let the guard's callback break the connection thread
                        report("row change notification failed: " + describe(exc))

        idl = _Idl(s.ovn_connection, helper, leader_only=True)
        conn = connection.Connection(idl, timeout=s.ovn_timeout)
        # ovsdbapp keeps the connection on the API *class* and sets it only once: a fresh subclass per attempt,
        # or a retry after a failed start would reuse the dead connection for good.
        api_cls = type("RouterAdvertisementNbApi", (impl_idl.OvnNbApiIdlImpl,), {"_ovsdb_connection": None})
        try:
            api = api_cls(conn)
        except Exception:
            try:
                idl.close()
            except Exception as exc:
                LOG.debug("closing the failed attempt's IDL: %s", describe(exc))
            raise
        return api, api.idl

    # state -----------------------------------------------------------------------------------------------------
    def connected(self):
        with self._lock:
            idl = self._idl
        if idl is None:
            return False
        # ovs.db.idl.Idl has no is_connected() (OVS 3.5); its jsonrpc session has. Prefer the IDL's own if a
        # version has one.
        probe = getattr(idl, "is_connected", None)
        if probe is None:
            probe = getattr(getattr(idl, "_session", None), "is_connected", None)
        if probe is None:
            self._report_once("the IDL offers no connection state (no is_connected on it or on its session)")
            return False
        try:
            return bool(probe())
        except Exception as exc:
            self._report_once("connection probe failed: " + describe(exc))
            return False

    def generation(self):
        """Changes whenever the connection drops or comes back (the guard re-scans on a change)."""
        with self._lock:
            idl = self._idl
        if idl is None:
            return None
        get_seqno = getattr(getattr(idl, "_session", None), "get_seqno", None)
        if get_seqno is None:
            self._report_once("the IDL's session offers no get_seqno: reconnects are seen only as a disconnection")
            return None
        try:
            return get_seqno()
        except Exception as exc:
            self._report_once("connection generation failed: " + describe(exc))
            return None

    # commands --------------------------------------------------------------------------------------------------
    def _run(self, step, *args):
        with self._lock:
            api = self._api
        if api is None or not self.connected():
            raise ovn_unavailable()
        from ovsdbapp.backend.ovs_idl import command

        class _Step(command.BaseCommand):
            def run_idl(self, txn):
                rows = list(self.api.idl.tables[LRP_TABLE].rows.values())
                self.result = step(rows, *args)

        try:
            return _Step(api).execute(check_error=True, log_errors=False)
        except ServiceError:
            raise
        except Exception as exc:
            LOG.warning("OVN transaction failed: %s", describe(exc))
            raise ovn_unavailable()

    def read_port(self, lrp):
        def read(rows, name):
            row = find_row(rows, name)
            if row is None:
                raise no_such_router_port(name)
            return port_state(row)
        return self._run(read, lrp)

    def set_nat(self, lrp, enabled):
        return self._run(lambda rows, name, on: step_set_nat(find_row(rows, name), name, on), lrp, enabled)

    def restore_nat(self, lrp):
        return self._run(lambda rows, name: step_restore(find_row(rows, name), name), lrp)

    def marked_ports(self):
        return self._run(scan_marked)


def describe(exc):
    """An exception for a log line: its class and text (never request headers; the callers pass none in)."""
    text = str(exc).replace("\n", " ")[:200]
    return "%s: %s" % (type(exc).__name__, text) if text else type(exc).__name__


# ---------------------------------------------------------------------------------------------------------------
# Keystone and Neutron, with the caller's own token

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the caller's token to another address: refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenStackClient:
    def __init__(self, keystone_url, neutron_url, cafile=None, timeout=10.0, opener=None):
        self.keystone_url = keystone_url
        self.neutron_url = neutron_url
        self.timeout = timeout
        if opener is None:
            context = ssl.create_default_context(cafile=cafile or None)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                                 urllib.request.HTTPSHandler(context=context))
        self._opener = opener

    def _get(self, url, headers, unavailable):
        request = urllib.request.Request(url, headers=dict(headers, Accept="application/json"), method="GET")
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            return status, None
        except (urllib.error.URLError, socket.timeout, OSError, ValueError, http.client.HTTPException) as exc:
            LOG.warning("%s: %s", unavailable().reason, describe(exc))
            raise unavailable()
        try:
            status = response.getcode()
            raw = response.read(MAX_RESPONSE + 1)
        except (socket.timeout, OSError, http.client.HTTPException) as exc:
            LOG.warning("%s: %s", unavailable().reason, describe(exc))
            raise unavailable()
        finally:
            response.close()
        if len(raw) > MAX_RESPONSE:
            raise unavailable("The answer of the service was too large.")
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise unavailable("The service answered with something that is not JSON.")

    def validate(self, token):
        """{user_id, project_id or None}: the token validated against Keystone by itself."""
        status, body = self._get(self.keystone_url + "/v3/auth/tokens?nocatalog",
                                 {"X-Auth-Token": token, "X-Subject-Token": token}, identity_unavailable)
        if status in (401, 403, 404):
            raise unauthenticated("Keystone does not accept the token.")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("token"), dict):
            raise identity_unavailable("The identity service answered %s." % status)
        tok = body["token"]
        user = tok.get("user") if isinstance(tok.get("user"), dict) else {}
        project = tok.get("project") if isinstance(tok.get("project"), dict) else {}
        return {"user_id": str(user.get("id") or ""), "project_id": project.get("id") or None}

    def port(self, token, port_id):
        """The Neutron port as the caller sees it; None when Neutron does not show it to the caller."""
        status, body = self._get(self.neutron_url + "/v2.0/ports/" + port_id, {"X-Auth-Token": token},
                                 network_unavailable)
        if status in (403, 404):
            return None
        if status == 401:
            raise unauthenticated("Neutron does not accept the token.")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("port"), dict):
            raise network_unavailable("The networking service answered %s." % status)
        return body["port"]


# ---------------------------------------------------------------------------------------------------------------
# The guard: holds `nat` on every marked port between the platform's calls.

def iso(ts):
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Guard:
    """Restores `nat` on marked ports. Woken by row changes (a bounded queue fed by the OVSDB thread), checks the
    connection generation for reconnects, and makes a full pass every `interval` seconds. Counters live in memory
    and start at zero; the wishes themselves are only in the database."""

    def __init__(self, store, interval=60.0, clock=time.time, limit=RESTORE_LIMIT, window=RESTORE_WINDOW):
        self.store = store
        self.interval = interval
        self.clock = clock
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._events = queue.Queue(maxsize=1024)
        self._rescan = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._wanted = set()
        self._history = collections.defaultdict(collections.deque)
        self._restores = collections.Counter()
        self._last = {}
        self._suspended = set()
        self._no_option_logged = set()
        self._total = 0
        self._generation = object()
        self._was_connected = False
        self._last_full = None

    # inputs ----------------------------------------------------------------------------------------------------
    def notify(self, name):
        """Called on the OVSDB thread for every Logical_Router_Port change: never blocks."""
        if not name:
            return
        try:
            self._events.put_nowait(name)
        except queue.Full:
            self._rescan.set()

    def lift(self, lrp):
        """The platform asked again for the port (PUT): its suspension and its rate window start over."""
        with self._lock:
            self._suspended.discard(lrp)
            self._history.pop(lrp, None)
            self._no_option_logged.discard(lrp)

    def forget(self, lrp):
        with self._lock:
            self._wanted.discard(lrp)
            self._suspended.discard(lrp)
            self._history.pop(lrp, None)
            self._no_option_logged.discard(lrp)

    def mark(self, lrp):
        with self._lock:
            self._wanted.add(lrp)

    # outputs ---------------------------------------------------------------------------------------------------
    def port_info(self, lrp):
        with self._lock:
            return {"restores": self._restores.get(lrp, 0), "lastRestoredAt": iso(self._last.get(lrp)),
                    "suspended": lrp in self._suspended}

    def summary(self):
        with self._lock:
            return {"wanted": len(self._wanted), "restoredSinceStart": self._total,
                    "suspended": sorted(port_id_of(n) for n in self._suspended)}

    # the work --------------------------------------------------------------------------------------------------
    def check(self, lrp, state):
        """One marked or unmarked port's state as read: restore `nat` when it is wanted and missing."""
        if state is None or not state.get("wanted"):
            self.forget(lrp)
            return None
        with self._lock:
            self._wanted.add(lrp)
            if state["redistribute"] is None:
                if lrp not in self._no_option_logged:
                    self._no_option_logged.add(lrp)
                    LOG.warning("guard: %s is marked but has no %s option; left alone", lrp, OPTION)
                return None
            self._no_option_logged.discard(lrp)
            if NAT in state["redistribute"] or lrp in self._suspended:
                return None
            now = self.clock()
            hist = self._history[lrp]
            while hist and now - hist[0] > self.window:
                hist.popleft()
            if len(hist) >= self.limit:
                self._suspended.add(lrp)
                LOG.error("guard: %s: nat removed again after %d restores in %.0f s; the guard stops restoring "
                          "this port until the platform asks for it again", lrp, len(hist), self.window)
                return None
        result = self.store.restore_nat(lrp)
        if result.get("changed"):
            with self._lock:
                now = self.clock()
                self._history[lrp].append(now)
                self._restores[lrp] += 1
                self._last[lrp] = now
                self._total += 1
            LOG.info("guard: restored nat on %s (before %s, after %s)", lrp, serialise_tokens(result["before"]),
                     serialise_tokens(result["after"]))
        elif result.get("outcome") in ("unmarked", "gone"):
            self.forget(lrp)
        return result

    def full_pass(self):
        marked = self.store.marked_ports()
        with self._lock:
            gone = self._wanted - set(marked)
        for lrp in gone:
            self.forget(lrp)
        for lrp in sorted(marked):
            try:
                self.check(lrp, marked[lrp])
            except ServiceError as exc:
                LOG.warning("guard: %s: %s", lrp, exc.message)
        self._last_full = self.clock()

    def handle_event(self, lrp):
        try:
            state = self.store.read_port(lrp)
        except ServiceError as exc:
            if exc.reason == "NoSuchRouterPort":
                self.forget(lrp)
                return
            raise
        self.check(lrp, state)

    def run_once(self, wait=1.0):
        """One turn of the guard thread: full pass when due (start, reconnect, interval, overflow), else events."""
        if not self.store.connected():
            self._was_connected = False
            self._stop.wait(min(wait, 1.0))
            return
        generation = self.store.generation()
        due = (self._last_full is None or not self._was_connected or generation != self._generation
               or self._rescan.is_set() or self.clock() - self._last_full >= self.interval)
        if due:
            self._rescan.clear()
            self._generation = generation
            self._was_connected = True
            self.full_pass()
            return
        try:
            name = self._events.get(timeout=wait)
        except queue.Empty:
            return
        names = {name}
        while True:
            try:
                names.add(self._events.get_nowait())
            except queue.Empty:
                break
        for lrp in sorted(names):
            self.handle_event(lrp)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except ServiceError as exc:
                LOG.warning("guard: %s", exc.message)
                self._stop.wait(1.0)
            except Exception as exc:  # the guard must not die
                LOG.error("guard: %s", describe(exc))
                self._stop.wait(1.0)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="guard", daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


# ---------------------------------------------------------------------------------------------------------------
# The API

class RouterAdvertisementService:
    def __init__(self, store, openstack, guard=None):
        self.store = store
        self.openstack = openstack
        self.guard = guard if guard is not None else Guard(store)

    def handle(self, method, path, headers, body):
        """(status, payload) for one request; headers is a case-insensitive mapping (or a dict)."""
        try:
            return self._handle(method, path, headers, body)
        except ServiceError as exc:
            return exc.status, {"error": exc.reason, "message": exc.message}
        except Exception as exc:
            LOG.error("internal error: %s", describe(exc))
            return 500, {"error": "InternalError", "message": "The service failed; see its log."}

    def _handle(self, method, path, headers, body):
        path = urllib.parse.urlsplit(path).path
        if path == "/healthz":
            if method != "GET":
                raise ServiceError(405, "MethodNotAllowed", "Use GET.")
            return 200, {"ok": True, "ovn": "connected" if self.store.connected() else "unreachable",
                         "guard": self.guard.summary()}
        if path == "/readyz":
            if method != "GET":
                raise ServiceError(405, "MethodNotAllowed", "Use GET.")
            if self.store.connected():
                return 200, {"ready": True, "ovn": "connected"}
            return 503, {"ready": False, "ovn": "unreachable"}
        for kind, rx in ROUTES:
            m = rx.match(path)
            if m:
                break
        else:
            raise ServiceError(404, "NotFound", "No such resource.")
        port_id = m.group(1)
        allowed = "GET" if kind == "port" else "PUT"
        if method != allowed:
            raise ServiceError(405, "MethodNotAllowed", "Use %s." % allowed)
        if not PORT_ID.match(port_id):
            raise bad_request("The port id must be a UUID in lower-case canonical form.")
        enabled = parse_body(body) if kind == "nat" else None
        caller = self.authorize(header(headers, "X-Auth-Token"), port_id)
        lrp = lrp_name(port_id)
        if kind == "port":
            state = self.store.read_port(lrp)
            if state["redistribute"] is None:
                raise not_advertising(lrp)
            info = self.guard.port_info(lrp)
            return 200, {"portId": port_id, "lrp": lrp, "redistribute": state["redistribute"],
                         "nat": NAT in state["redistribute"], "wanted": state["wanted"],
                         "restores": info["restores"], "lastRestoredAt": info["lastRestoredAt"]}
        result = self.store.set_nat(lrp, enabled)
        if enabled:
            self.guard.lift(lrp)
            self.guard.mark(lrp)
        else:
            self.guard.forget(lrp)
        if result["changed"]:
            LOG.info("write: user %s project %s port %s before %s after %s wanted %s", caller["user_id"],
                     caller["project_id"], port_id, serialise_tokens(result["before"]),
                     serialise_tokens(result["after"]), "yes" if enabled else "no")
        return 200, {"portId": port_id, "lrp": lrp, "before": result["before"], "after": result["after"],
                     "changed": result["changed"]}

    def authorize(self, token, port_id):
        if not token or not TOKEN.match(token):
            raise unauthenticated()
        caller = self.openstack.validate(token)
        if not caller.get("project_id"):
            raise ServiceError(403, "NotProjectScoped", "The token is not scoped to a project.")
        port = self.openstack.port(token, port_id)
        project = None if port is None else (port.get("project_id") or port.get("tenant_id"))
        if port is None or project != caller["project_id"]:
            raise ServiceError(403, "NotYourPort", "The port is not a port of the token's project.")
        if port.get("device_owner") not in ROUTER_INTERFACE_OWNERS:
            raise ServiceError(403, "NotARouterInterface", "The port is not a router interface.")
        return caller


def header(headers, name):
    if headers is None:
        return None
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is not None:
            return value
    for key in headers:
        if key.lower() == name.lower():
            return headers[key]
    return None


def parse_body(body):
    if body is None or len(body) == 0:
        raise bad_request('The body must be {"enabled": true} or {"enabled": false}.')
    if len(body) > MAX_BODY:
        raise bad_request("The body is too large.")
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (ValueError, UnicodeDecodeError):
        raise bad_request("The body is not JSON.")
    if not isinstance(data, dict) or set(data) != {"enabled"} or not isinstance(data["enabled"], bool):
        raise bad_request('The body must be {"enabled": true} or {"enabled": false}.')
    return data["enabled"]


# ---------------------------------------------------------------------------------------------------------------
# HTTP

def make_handler(service, request_timeout=15.0):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "router-advertisement"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = request_timeout

        def setup(self):
            super().setup()
            if isinstance(self.connection, ssl.SSLSocket):
                self.connection.do_handshake()

        def _serve(self, method):
            started = time.monotonic()
            body = None
            # a body is read only for PUT; a request that carries one otherwise (or a chunked one) gets its answer and
            # the connection closes, so the unread bytes are never parsed as the next request on the connection
            length = (self.headers.get("Content-Length") or "").strip()
            unread = bool(self.headers.get("Transfer-Encoding")) or (length not in ("", "0") and method != "PUT")
            if method == "PUT" and self.headers.get("Transfer-Encoding"):
                self._reply(400, {"error": "BadRequest", "message": "The body needs a Content-Length, not a "
                                  "Transfer-Encoding."}, started, method, close=True)
                return
            if method == "PUT":
                length = self.headers.get("Content-Length")
                if length is None or not length.isdigit() or int(length) > MAX_BODY:
                    status, payload = 400, {"error": "BadRequest",
                                            "message": "The body needs a Content-Length of at most %d." % MAX_BODY}
                    self._reply(status, payload, started, method, close=True)
                    return
                body = self.rfile.read(int(length))
            status, payload = service.handle(method, self.path, self.headers, body)
            self._reply(status, payload, started, method, close=unread)

        def _reply(self, status, payload, started, method, close=False):
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)
            path = urllib.parse.urlsplit(self.path).path
            if path not in ("/healthz", "/readyz") or status not in (200, 503):
                LOG.info("%s %s %d %.0f ms", method, path[:200], status, (time.monotonic() - started) * 1000)

        def do_GET(self):
            self._serve("GET")

        def do_PUT(self):
            self._serve("PUT")

        def do_POST(self):
            self._serve("POST")

        def do_DELETE(self):
            self._serve("DELETE")

        def do_PATCH(self):
            self._serve("PATCH")

        def log_message(self, fmt, *args):  # the request line and errors of http.server: never the headers
            LOG.debug("http: " + fmt, *args)

    return Handler


class Server(http.server.ThreadingHTTPServer):
    """One thread per connection, at most `max_connections` at a time (a connection beyond that is closed at once),
    and no connection lives longer than `deadline` seconds: the per-read timeout alone let a client that sends one
    byte every few seconds hold a thread for as long as it liked."""
    daemon_threads = False
    block_on_close = True  # server_close() waits for the requests in flight
    request_queue_size = 128  # the listen backlog: the default of 5 resets connections under a burst
    tls = None  # (context, certificate file, key file) when the server serves TLS
    reload_interval = 60.0
    _tls_checked = 0.0
    _tls_stamp = None

    def __init__(self, address, handler, bind_and_activate=True, max_connections=None, deadline=None,
                 clock=time.monotonic):
        self.max_connections = max_connections or MAX_CONNECTIONS
        self.deadline = deadline or CONNECTION_DEADLINE
        self.clock = clock
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._open = {}
        self._open_lock = threading.Lock()
        super().__init__(address, handler, bind_and_activate)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            LOG.warning("connection from %s refused: %d connections are open", client_address[0], self.max_connections)
            self.shutdown_request(request)
            return
        with self._open_lock:
            self._open[id(request)] = (request, self.clock())
        try:
            super().process_request(request, client_address)
        except Exception:
            self._done(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._done(request)

    def _done(self, request):
        with self._open_lock:
            if self._open.pop(id(request), None) is None:
                return
        self._slots.release()

    def service_actions(self):
        """Called by serve_forever between polls: cut the connections older than the deadline."""
        now = self.clock()
        with self._open_lock:
            late = [sock for sock, since in self._open.values() if now - since > self.deadline]
        for sock in late:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def get_request(self):
        self.maybe_reload_tls()
        return super().get_request()

    def maybe_reload_tls(self, now=None):
        """Load a renewed serving certificate (a mounted Secret changes in place): new connections get it."""
        if self.tls is None:
            return False
        now = time.monotonic() if now is None else now
        if now - self._tls_checked < self.reload_interval:
            return False
        self._tls_checked = now
        context, cert, key = self.tls
        try:
            stamp = (os.stat(cert).st_mtime_ns, os.stat(key).st_mtime_ns)
        except OSError as exc:
            LOG.warning("serving certificate: %s", describe(exc))
            return False
        if stamp == self._tls_stamp:
            return False
        try:
            context.load_cert_chain(cert, key)
        except (OSError, ssl.SSLError) as exc:
            LOG.error("serving certificate not reloaded: %s", describe(exc))
            return False
        self._tls_stamp = stamp
        LOG.info("serving certificate reloaded")
        return True

    def handle_error(self, request, client_address):
        LOG.warning("connection from %s: %s", client_address[0], describe(sys.exc_info()[1]))


def build_server(service, address, port, tls_cert=None, tls_key=None, max_connections=None, deadline=None):
    server = Server((address, port), make_handler(service), max_connections=max_connections, deadline=deadline)
    if tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(tls_cert, tls_key)
        # The handshake runs in the request's thread (setup), not in the accept loop.
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        server.tls = (context, tls_cert, tls_key)
        server._tls_stamp = (os.stat(tls_cert).st_mtime_ns, os.stat(tls_key).st_mtime_ns)
        server._tls_checked = time.monotonic()
    return server


class ProbeOnly:
    """The service's /healthz and /readyz, and nothing else (the probe listener)."""

    def __init__(self, service):
        self.service = service

    def handle(self, method, path, headers, body):
        if urllib.parse.urlsplit(path).path not in PROBE_PATHS:
            return 404, {"error": "NotFound", "message": "This port answers /healthz and /readyz only."}
        return self.service.handle(method, path, {}, None)


def build_probe_server(service, address, port):
    """The probes' own plain HTTP listener: a client holding every slot of the service's listener (or this one's few,
    for at most PROBE_DEADLINE seconds) never keeps the kubelet's probes waiting for long."""
    return Server((address, port), make_handler(ProbeOnly(service), request_timeout=5.0),
                  max_connections=PROBE_CONNECTIONS, deadline=PROBE_DEADLINE)


# ---------------------------------------------------------------------------------------------------------------
# Settings

class Settings:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _number(env, name, default, cast, low, high):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError("%s: not a number: %r" % (name, raw))
    if not low <= value <= high:
        raise ConfigError("%s: must be between %s and %s" % (name, low, high))
    return value


def _url(env, name):
    raw = (env.get(name) or "").strip()
    if not raw:
        raise ConfigError("%s: required setting is missing" % name)
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        raise ConfigError("%s: must be an http or https URL without query: %r" % (name, raw))
    return raw.rstrip("/")


def _file(env, name, required):
    raw = (env.get(name) or "").strip()
    if not raw:
        if required:
            raise ConfigError("%s: required setting is missing" % name)
        return None
    if not os.path.isfile(raw):
        raise ConfigError("%s: no such file: %s" % (name, raw))
    return raw


def load_settings(env=None):
    env = os.environ if env is None else env
    ovn = (env.get("OVN_NB_CONNECTION") or "").strip()
    if not ovn:
        raise ConfigError("OVN_NB_CONNECTION: required setting is missing")
    remotes = [r.strip() for r in ovn.split(",")]
    for remote in remotes:
        if not re.match(r"^(ssl|tcp):[A-Za-z0-9.\-\[\]:]+:\d{1,5}$", remote):
            raise ConfigError("OVN_NB_CONNECTION: each remote must be ssl:<host>:<port> or tcp:<host>:<port>: %r"
                              % remote)
    ovn_ssl = any(r.startswith("ssl:") for r in remotes)
    keystone = _url(env, "KEYSTONE_URL")
    keystone = re.sub(r"/v3$", "", keystone)
    neutron = _url(env, "NEUTRON_URL")
    neutron = re.sub(r"/v2\.0$", "", neutron)
    tls_cert = _file(env, "TLS_CERTIFICATE", False)
    tls_key = _file(env, "TLS_PRIVATE_KEY", bool(tls_cert))
    if tls_key and not tls_cert:
        raise ConfigError("TLS_CERTIFICATE: required setting is missing (TLS_PRIVATE_KEY is set)")
    address = (env.get("LISTEN_ADDRESS") or "").strip() or "0.0.0.0"
    listen_port = _number(env, "LISTEN_PORT", 8080, int, 1, 65535)
    probe_port = _number(env, "PROBE_PORT", None, int, 1, 65535)
    if probe_port is not None and probe_port == listen_port:
        raise ConfigError("PROBE_PORT: must differ from LISTEN_PORT (%d)" % listen_port)
    level = (env.get("LOG_LEVEL") or "").strip().upper() or "INFO"
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("LOG_LEVEL: must be DEBUG, INFO, WARNING or ERROR")
    return Settings(
        ovn_connection=",".join(remotes), ovn_ssl=ovn_ssl,
        ovn_private_key=_file(env, "OVN_NB_PRIVATE_KEY", ovn_ssl),
        ovn_certificate=_file(env, "OVN_NB_CERTIFICATE", ovn_ssl),
        ovn_ca_cert=_file(env, "OVN_NB_CA_CERT", ovn_ssl),
        keystone_url=keystone, neutron_url=neutron, cacert=_file(env, "OS_CACERT", False),
        listen_address=address, listen_port=listen_port, probe_port=probe_port,
        tls_certificate=tls_cert, tls_private_key=tls_key,
        ovn_timeout=_number(env, "OVN_TIMEOUT_SECONDS", 10.0, float, 1, 300),
        api_timeout=_number(env, "API_TIMEOUT_SECONDS", 10.0, float, 1, 300),
        guard_interval=_number(env, "GUARD_INTERVAL_SECONDS", 60.0, float, 5, 86400),
        max_connections=_number(env, "MAX_CONNECTIONS", MAX_CONNECTIONS, int, 1, 1024),
        connection_deadline=_number(env, "CONNECTION_DEADLINE_SECONDS", CONNECTION_DEADLINE, float, 5, 3600),
        log_level=level,
    )


# ---------------------------------------------------------------------------------------------------------------
# Main

def main(argv=None, env=None):
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings(env)
    except ConfigError as exc:
        LOG.error("refusing to start: %s", exc)
        return 2
    logging.getLogger().setLevel(settings.log_level)
    store = OvsdbappPortStore(settings)
    guard = Guard(store, interval=settings.guard_interval)
    openstack = OpenStackClient(settings.keystone_url, settings.neutron_url, settings.cacert, settings.api_timeout)
    service = RouterAdvertisementService(store, openstack, guard)
    try:
        server = build_server(service, settings.listen_address, settings.listen_port, settings.tls_certificate,
                              settings.tls_private_key, getattr(settings, "max_connections", None),
                              getattr(settings, "connection_deadline", None))
    except (OSError, ssl.SSLError) as exc:
        LOG.error("refusing to start: cannot listen on %s:%d: %s", settings.listen_address, settings.listen_port,
                  describe(exc))
        return 2
    probes = None
    if getattr(settings, "probe_port", None):
        try:
            probes = build_probe_server(service, settings.listen_address, settings.probe_port)
        except OSError as exc:
            LOG.error("refusing to start: cannot listen on %s:%d: %s", settings.listen_address, settings.probe_port,
                      describe(exc))
            server.server_close()
            return 2
    store.start(on_change=guard.notify)
    guard.start()
    stopping = threading.Event()

    def on_signal(signum, frame):
        stopping.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    worker = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    worker.start()
    if probes is not None:
        threading.Thread(target=probes.serve_forever, name="probes", daemon=True).start()
        LOG.info("probes on %s:%d (http)", settings.listen_address, settings.probe_port)
    LOG.info("listening on %s:%d (%s)", settings.listen_address, settings.listen_port,
             "https" if settings.tls_certificate else "http")
    while not stopping.wait(1.0):
        pass
    LOG.info("stopping: finishing the requests in flight")
    server.shutdown()
    server.server_close()
    if probes is not None:
        probes.shutdown()
        probes.server_close()
    guard.stop()
    store.stop()
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
