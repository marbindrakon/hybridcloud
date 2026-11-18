"""A Kubernetes API over HTTP for the real kubernetes.core modules: version and API discovery, OpenShift Routes (the
list of a namespace with equality label selectors applied by the server, as the API server does) and, given as
`objects`, objects of the platform's kinds (hybridsovereign.redhat/v1alpha1): list per namespace, get by name, and a
merge PATCH of metadata.annotations (recorded in `patches`). Requests are recorded with their path, query and bearer
token."""
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
PLATFORM = "hybridsovereign.redhat/v1alpha1"
PLURALS = {"Entity": "entities", "Team": "teams", "Project": "projects", "CloudOSO": "cloudosos",
           "CloudVirt": "cloudvirts", "Rbac": "rbacs"}
PLATFORM_RESOURCES = {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": PLATFORM,
                      "resources": [{"name": p, "singularName": k.lower(), "namespaced": True, "kind": k,
                                     "verbs": ["get", "list", "patch", "update", "watch"]} for k, p in PLURALS.items()]}
GROUPS = {"kind": "APIGroupList", "apiVersion": "v1", "groups": [
    {"name": "route.openshift.io", "versions": [{"groupVersion": "route.openshift.io/v1", "version": "v1"}],
     "preferredVersion": {"groupVersion": "route.openshift.io/v1", "version": "v1"}},
    {"name": "hybridsovereign.redhat", "versions": [{"groupVersion": PLATFORM, "version": "v1alpha1"}],
     "preferredVersion": {"groupVersion": PLATFORM, "version": "v1alpha1"}}]}


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
    def __init__(self, routes=(), objects=()):
        self.routes = list(routes)
        self.objects = [dict(o, apiVersion=PLATFORM) for o in objects]
        self.requests = []
        self.patches = []
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
                if p == "/apis/" + PLATFORM:
                    return self._send(200, PLATFORM_RESOURCES)
                obj = self._platform(parts)
                if obj is not None:
                    return self._send(*obj)
                return self._send(404, {"kind": "Status", "status": "Failure", "reason": "NotFound", "code": 404})

            def _platform(self, parts):
                # /apis/hybridsovereign.redhat/v1alpha1/namespaces/<ns>/<plural>[/<name>]
                if len(parts) < 7 or "/".join(parts[2:4]) != PLATFORM or parts[4] != "namespaces":
                    return None
                kind = next((k for k, pl in PLURALS.items() if pl == parts[6]), None)
                if kind is None:
                    return None
                items = [o for o in fake.objects if o["kind"] == kind and o["metadata"]["namespace"] == parts[5]]
                if len(parts) == 7:
                    return 200, {"kind": kind + "List", "apiVersion": PLATFORM, "metadata": {"resourceVersion": "1"},
                                 "items": items}
                hit = [o for o in items if o["metadata"]["name"] == parts[7]]
                return (200, hit[0]) if hit else (404, {"kind": "Status", "status": "Failure", "reason": "NotFound",
                                                        "code": 404})

            def do_PATCH(self):
                url = urllib.parse.urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.requests.append({"path": url.path, "query": {}, "auth": self.headers.get("Authorization")})
                if "strategic-merge-patch" in (self.headers.get("Content-Type") or ""):
                    return self._send(415, {"kind": "Status", "status": "Failure", "code": 415,
                                            "reason": "UnsupportedMediaType"})
                got = self._platform(url.path.rstrip("/").split("/"))
                if not got or got[0] != 200 or "items" in got[1]:
                    return self._send(404, {"kind": "Status", "status": "Failure", "reason": "NotFound", "code": 404})
                obj = got[1]
                ann = ((body.get("metadata") or {}).get("annotations")) or {}
                obj["metadata"].setdefault("annotations", {}).update(ann)
                fake.patches.append({"kind": obj["kind"], "namespace": obj["metadata"]["namespace"],
                                     "name": obj["metadata"]["name"], "annotations": ann})
                return self._send(200, obj)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def route_lists(self):
        return [r for r in self.requests if r["path"].endswith("/routes")]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
