"""A Kubernetes API over HTTP with OpenShift Routes only, for the real kubernetes.core.k8s_info module: version and
API discovery, and the Route list of a namespace with equality label selectors applied by the server (as the API
server does). Requests are recorded with their path, query and bearer token."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROUTE_RESOURCES = {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": "route.openshift.io/v1",
                   "resources": [{"name": "routes", "singularName": "route", "namespaced": True, "kind": "Route",
                                  "verbs": ["create", "delete", "get", "list", "patch", "update", "watch"]},
                                 {"name": "routes/status", "singularName": "", "namespaced": True, "kind": "Route",
                                  "verbs": ["get", "patch", "update"]}]}
CORE_RESOURCES = {"kind": "APIResourceList", "groupVersion": "v1",
                  "resources": [{"name": "namespaces", "singularName": "namespace", "namespaced": False,
                                 "kind": "Namespace", "verbs": ["get", "list"]}]}
GROUPS = {"kind": "APIGroupList", "apiVersion": "v1", "groups": [
    {"name": "route.openshift.io", "versions": [{"groupVersion": "route.openshift.io/v1", "version": "v1"}],
     "preferredVersion": {"groupVersion": "route.openshift.io/v1", "version": "v1"}}]}


def route(name, host, labels=None, namespace="services-rhbk"):
    return {"apiVersion": "route.openshift.io/v1", "kind": "Route",
            "metadata": {"name": name, "namespace": namespace, "labels": dict(labels or {})},
            "spec": {"host": host, "to": {"kind": "Service", "name": "kc-service"}}}


def _selected(obj, selector):
    labels = obj["metadata"].get("labels") or {}
    for term in [t for t in (selector or "").split(",") if t]:
        key, _, value = term.partition("=")
        if labels.get(key.strip()) != value.strip():
            return False
    return True


class FakeK8s:
    def __init__(self, routes=()):
        self.routes = list(routes)
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(url.query)
                fake.requests.append({"path": url.path, "query": q, "auth": self.headers.get("Authorization")})
                p = url.path.rstrip("/")
                if p == "/version":
                    return self._send(200, {"major": "1", "minor": "35", "gitVersion": "v1.35.0"})
                if p == "/api":
                    return self._send(200, {"kind": "APIVersions", "versions": ["v1"]})
                if p == "/api/v1":
                    return self._send(200, CORE_RESOURCES)
                if p == "/apis":
                    return self._send(200, GROUPS)
                if p == "/apis/route.openshift.io/v1":
                    return self._send(200, ROUTE_RESOURCES)
                parts = p.split("/")
                # /apis/route.openshift.io/v1/namespaces/<ns>/routes
                if len(parts) == 7 and parts[4] == "namespaces" and parts[6] == "routes":
                    sel = (q.get("labelSelector") or [""])[0]
                    items = [r for r in fake.routes if r["metadata"]["namespace"] == parts[5] and _selected(r, sel)]
                    return self._send(200, {"kind": "RouteList", "apiVersion": "route.openshift.io/v1",
                                            "metadata": {"resourceVersion": "1"}, "items": items})
                return self._send(404, {"kind": "Status", "status": "Failure", "reason": "NotFound", "code": 404})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def route_lists(self):
        return [r for r in self.requests if r["path"].endswith("/routes")]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
