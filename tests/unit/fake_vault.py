"""A Vault KV v2 over HTTP for role tests (the jobs' vault_kv_read / vault_kv_write / vault_kv_delete task files talk to
it with ansible.builtin.uri): GET and POST /v1/<mount>/data/<path>, DELETE /v1/<mount>/metadata/<path>. A request
without the token is refused (403). Every request is logged as (method, path, status); bodies are kept only in the
store (a test that greps output for secrets must not find them here by accident)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Vault:
    def __init__(self, token="test-vault-token"):
        self.token = token
        self.data = {}
        self.log = []

    def start(self):
        v = self

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
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln) if ln else b""
                parts = self.path.split("/", 4)   # '', 'v1', mount, kind, path
                if self.headers.get("X-Vault-Token") != v.token:
                    v.log.append((method, self.path, 403))
                    return self._send(403, {"errors": ["permission denied"]})
                if len(parts) < 5:
                    return self._send(404, {"errors": []})
                _, _, mount, kind, path = parts
                key = f"{mount}/{path}"
                if method == "GET" and kind == "data":
                    if key not in v.data:
                        status, body = 404, {"errors": []}
                    else:
                        status, body = 200, {"data": {"data": v.data[key], "metadata": {"version": 1}}}
                elif method in ("POST", "PUT") and kind == "data":
                    v.data[key] = json.loads(raw or b"{}").get("data") or {}
                    status, body = 200, {"data": {"version": 1}}
                elif method == "DELETE" and kind == "metadata":
                    status, body = (204, None) if v.data.pop(key, None) is not None else (404, {"errors": []})
                else:
                    status, body = 405, {"errors": []}
                v.log.append((method, self.path, status))
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
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
