"""A small sign-in directory admin API over HTTPS (a self-signed certificate; the jobs talk to it with
validate_certs false) for the role tests that run the real common/tasks/services_signin_groups.yml and
obtain_keycloak_realm_token.yml: the client-credentials token endpoint of a realm, clients by clientId, a client's
roles with their attributes (list, create 201 / 409, read, update 204, delete 204), group-by-path and a group's client
role mappings. Requests without the realm token are refused (401). Every request and its status is in `log`;
`down` makes every request fail at the connection (the directory unreachable)."""
import datetime
import json
import os
import secrets
import ssl
import tempfile
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _self_signed(tmp):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    cf, kf = os.path.join(tmp, "cert.pem"), os.path.join(tmp, "key.pem")
    with open(cf, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kf, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cf, kf


class Keycloak:
    def __init__(self, realm="sovereign-tenants"):
        self.realm = realm
        self.clients = {}        # clientId -> {id, clientId, roles: {name: role}}
        self.groups = {}         # path (no leading slash) -> {id, path, mappings: {client uuid: [role names]}}
        self.automation = {"clients": ("kc-clients", secrets.token_urlsafe(12)),
                           "membership": ("kc-membership", secrets.token_urlsafe(12))}
        self.tokens = set()
        self.log = []
        self.down = False
        self.fail_next = []      # [(method, path prefix, status)] answered once

    def add_client(self, client_id):
        cid = uuid.uuid4().hex
        self.clients[client_id] = {"id": cid, "clientId": client_id, "roles": {}}
        return cid

    def add_group(self, path):
        gid = uuid.uuid4().hex
        self.groups[path.strip("/")] = {"id": gid, "path": "/" + path.strip("/"), "mappings": {}}
        return gid

    def role(self, client_id, name):
        return self.clients[client_id]["roles"].get(name)

    def vault_entry(self):
        """What rbacconfig_provision publishes in Vault for the realm's automation clients."""
        return {"clientsClientId": self.automation["clients"][0], "clientsClientSecret": self.automation["clients"][1],
                "membershipClientId": self.automation["membership"][0],
                "membershipClientSecret": self.automation["membership"][1], "realm": self.realm, "url": self.url}

    def start(self):
        kc = self
        self.tmp = tempfile.mkdtemp(prefix="shc-fake-kc-")
        cf, kf = _self_signed(self.tmp)

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=None):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def _do(self, method):
                if kc.down:
                    self.connection.close()
                    return
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln) if ln else b""
                status, body = kc.handle(method, self.path, dict(self.headers), raw)
                kc.log.append((method, self.path.split("?")[0], status))
                self._send(status, body)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_PUT(self):
                self._do("PUT")

            def do_DELETE(self):
                self._do("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cf, kf)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        self.url = "https://" + self.host
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def handle(self, method, path, headers, raw):
        u = urllib.parse.urlsplit(path)
        p = [urllib.parse.unquote(x) for x in u.path.strip("/").split("/")]
        for i, (m, prefix, st) in enumerate(self.fail_next):
            if m == method and u.path.startswith(prefix):
                del self.fail_next[i]
                return st, {"error": "fake: unavailable"}
        if method == "POST" and p[:2] == ["realms", self.realm] and p[2:] == ["protocol", "openid-connect", "token"]:
            form = dict(urllib.parse.parse_qsl(raw.decode()))
            if (form.get("client_id"), form.get("client_secret")) not in self.automation.values():
                return 401, {"error": "invalid_client"}
            tok = "kc-" + secrets.token_hex(12)
            self.tokens.add(tok)
            return 200, {"access_token": tok, "token_type": "Bearer", "expires_in": 300}
        if headers.get("Authorization", "")[7:] not in self.tokens:
            return 401, {"error": "HTTP 401 Unauthorized"}
        if p[:3] != ["admin", "realms", self.realm]:
            return 404, {"error": "Realm not found."}
        rest = p[3:]
        q = dict(urllib.parse.parse_qsl(u.query))
        body = json.loads(raw) if raw else None
        if rest == ["clients"] and method == "GET":
            c = self.clients.get(q.get("clientId"))
            return 200, ([{"id": c["id"], "clientId": c["clientId"]}] if c else [])
        if len(rest) >= 3 and rest[0] == "clients" and rest[2] == "roles":
            client = next((c for c in self.clients.values() if c["id"] == rest[1]), None)
            if client is None:
                return 404, {"error": "Could not find client"}
            roles = client["roles"]
            if len(rest) == 3 and method == "GET":
                return 200, list(roles.values())
            if len(rest) == 3 and method == "POST":
                if body["name"] in roles:
                    return 409, {"errorMessage": "Role with name exists"}
                roles[body["name"]] = {"id": uuid.uuid4().hex, "name": body["name"],
                                       "description": body.get("description", ""), "clientRole": True,
                                       "containerId": client["id"], "attributes": body.get("attributes") or {}}
                return 201, None
            r = roles.get(rest[3]) if len(rest) == 4 else None
            if r is None:
                return 404, {"error": "Could not find role"}
            if method == "GET":
                return 200, r
            if method == "PUT":
                r.update({"description": body.get("description", r["description"]),
                          "attributes": body.get("attributes") or {}})
                return 204, None
            if method == "DELETE":
                del roles[rest[3]]
                for g in self.groups.values():
                    g["mappings"][client["id"]] = [n for n in g["mappings"].get(client["id"], []) if n != rest[3]]
                return 204, None
        if rest and rest[0] == "group-by-path" and method == "GET":
            g = self.groups.get("/".join(rest[1:]).strip("/"))
            return (200, {"id": g["id"], "path": g["path"]}) if g else (404, {"error": "Group path does not exist"})
        if len(rest) == 5 and rest[0] == "groups" and rest[2:4] == ["role-mappings", "clients"] and method == "POST":
            g = next((x for x in self.groups.values() if x["id"] == rest[1]), None)
            if g is None:
                return 404, {"error": "Could not find group"}
            m = g["mappings"].setdefault(rest[4], [])
            for r in body or []:
                if r["name"] not in m:
                    m.append(r["name"])
            return 204, None
        return 404, {"error": "fake: no such resource"}
