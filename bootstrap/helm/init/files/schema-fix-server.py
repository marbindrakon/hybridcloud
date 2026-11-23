"""Argo CD OpenAPI v2 schema compatibility shim: the HTTPS server behind the APIService
v1.schemafix.sovereign-bootstrap.io.

Argo CD's kube-openapi looks up ~1-encoded $ref keys of the aggregated /openapi/v2 literally. This server finds the
definitions those references need in the live cluster's OpenAPI documents and serves them under their ~1-encoded
keys, so the aggregated document resolves.

Each connection is served in its own thread, and its TLS handshake runs there too, bounded by a timeout, and so are
reads: one peer that connects and then stays silent cannot block the server. /healthz answers through the same path
as every other request, so a liveness probe on it sees a server that no longer answers.
"""
import http.server
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.request

KUBE_API = os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')
KUBE_PORT = os.environ.get('KUBERNETES_SERVICE_PORT', '443')
TOKEN_FILE = '/var/run/secrets/kubernetes.io/serviceaccount/token'
CA_FILE = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'


def env_float(name, default):
    value = os.environ.get(name, '')
    return float(value) if value else default


def kube_get(path, timeout=120):
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(CA_FILE)
    req = urllib.request.Request(
        'https://%s:%s%s' % (KUBE_API, KUBE_PORT, path),
        headers={'Authorization': 'Bearer %s' % token}
    )
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return json.loads(r.read())


def load_shims(get=kube_get):
    """Build shim definitions by:
    1. Scanning aggregated /openapi/v2 for ~1-encoded $ref keys not in definitions
    2. Scanning per-group /openapi/v3 for all schema names and adding tilde-encoded stubs
       (v3 is not affected by our own contributions to the v2 aggregated spec)
    """
    v2_spec = get('/openapi/v2')
    v2_defs = v2_spec.get('definitions', {})
    v2_str = json.dumps(v2_spec)
    all_v2_refs = set(re.findall(r'"#/definitions/([^"]+)"', v2_str))

    shims = {}

    # Step 1: from aggregated v2, every tilde-encoded $ref key missing from the definitions
    for tilde_key in all_v2_refs:
        if '~1' not in tilde_key:
            continue
        slash_key = tilde_key.replace('~1', '/').replace('~0', '~')
        if slash_key in v2_defs:
            # the slash-decoded definition under the tilde-encoded key
            shims[tilde_key] = v2_defs[slash_key]

    # Step 2: from per-group v3, the schema names of API groups that have slash-separated definition names
    # (these are the ones that cause ~1 reference failures)
    try:
        v3_index = get('/openapi/v3')
        for path_info in v3_index.get('paths', {}).values():
            group_path = path_info.get('serverRelativeURL', '')
            if not group_path:
                continue
            try:
                v3_spec = get(group_path.split('?')[0])
                for schema_name in v3_spec.get('components', {}).get('schemas', {}).keys():
                    if '/' not in schema_name:
                        continue
                    tilde_key = schema_name.replace('~', '~0').replace('/', '~1')
                    if tilde_key not in shims:
                        if schema_name in v2_defs:
                            shims[tilde_key] = v2_defs[schema_name]
                        else:
                            shims[tilde_key] = {
                                'type': 'object',
                                'x-kubernetes-preserve-unknown-fields': True
                            }
            except Exception:
                pass
    except Exception as e:
        print('v3 group scan failed (non-fatal): %s' % e, flush=True)

    return shims


class Shims:
    """The definitions served, replaced as a whole by the refresh loop."""

    def __init__(self, definitions=None):
        self._lock = threading.Lock()
        self._definitions = dict(definitions or {})

    def get(self):
        with self._lock:
            return dict(self._definitions)

    def set(self, definitions):
        with self._lock:
            self._definitions = dict(definitions)


def refresh_loop(shims, first_delay=90, interval=120):
    time.sleep(first_delay)  # let an old pod terminate and the API aggregation cache refresh
    while True:
        try:
            s = load_shims()
            shims.set(s)
            print('Schema shims refreshed: %d definitions' % len(s), flush=True)
        except Exception as e:
            print('Shim refresh failed: %s' % e, flush=True)
        time.sleep(interval)


class Handler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        # StreamRequestHandler applies self.timeout to the connection: every read and write is bounded
        self.timeout = self.server.read_timeout
        super().setup()

    def do_GET(self):
        if self.path.split('?')[0] == '/healthz':
            body = b'ok'
            ctype = 'text/plain'
        elif '/openapi/v2' in self.path:
            body = json.dumps({
                'swagger': '2.0',
                'info': {'title': 'ArgoCD Schema Compatibility Shim', 'version': '0.2.0'},
                'paths': {
                    '/apis/schemafix.sovereign-bootstrap.io/v1/': {'get': {
                        'operationId': 'listSchemaFixResources',
                        'produces': ['application/json'],
                        'responses': {'200': {'description': 'OK'}}
                    }}
                },
                'definitions': self.server.shims.get()
            }).encode()
            ctype = 'application/json'
        else:
            body = b'{"apiVersion":"v1","groupVersion":"schemafix.sovereign-bootstrap.io/v1","resources":[]}'
            ctype = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TLSThreadingServer(http.server.ThreadingHTTPServer):
    """An HTTPS server that accepts plain TCP connections and does each TLS handshake in the connection's own
    thread, with a timeout, so the accepting thread never waits on a peer."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, tls_context, shims, handshake_timeout=10.0, read_timeout=30.0, max_connections=64):
        self.tls_context = tls_context
        self.shims = shims
        self.handshake_timeout = handshake_timeout
        self.read_timeout = read_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        # at most max_connections connections at once: past that a connection is closed at once instead of
        # starting one more thread
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(self.handshake_timeout)
            try:
                tls = self.tls_context.wrap_socket(request, server_side=True)
            except (OSError, ValueError):
                # a handshake that timed out or failed: this connection only
                self.shutdown_request(request)
                return
            super().process_request_thread(tls, client_address)
        finally:
            self._slots.release()


def make_server(address, certfile, keyfile, shims, handshake_timeout=10.0, read_timeout=30.0, max_connections=64):
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(certfile, keyfile)
    return TLSThreadingServer(address, tls_context, shims, handshake_timeout, read_timeout, max_connections)


def main():
    shims = Shims()
    try:
        shims.set(load_shims())
        print('Initial shims loaded: %d definitions' % len(shims.get()), flush=True)
    except Exception as e:
        print('Initial shim load failed (will retry): %s' % e, flush=True)
    threading.Thread(target=refresh_loop, args=(shims,), daemon=True).start()

    httpd = make_server(('', int(os.environ.get('SCHEMA_FIX_PORT', '6443'))),
                        os.environ.get('SCHEMA_FIX_CERT', '/tmp/certs/server.crt'),
                        os.environ.get('SCHEMA_FIX_KEY', '/tmp/certs/server.key'),
                        shims,
                        handshake_timeout=env_float('SCHEMA_FIX_HANDSHAKE_TIMEOUT', 10.0),
                        read_timeout=env_float('SCHEMA_FIX_READ_TIMEOUT', 30.0),
                        max_connections=int(os.environ.get('SCHEMA_FIX_MAX_CONNECTIONS', '') or 64))
    print('Schema fix server ready on :%d' % httpd.server_address[1], flush=True)
    httpd.serve_forever()


if __name__ == '__main__':
    sys.exit(main())
