"""The Keycloak chart's own Routes (bootstrap/helm/charts/rhbk: keycloak.ingress.enabled, keycloak.routes) and the
central chart's Application of the tenant Keycloak, rendered with helm template.

Judged on the rendered objects: by default the chart renders what it rendered before (no Route, no Certificate, no
spec.ingress); with routes, exactly the primary Route carries the label app=keycloak by which the automation finds
the Keycloak, a certificate is ECDSA 256 unless set, served through externalCertificate with the router's read access
to that Secret, and the sync waves put the Certificates after the Keycloak and the Routes after the Certificates;
the combinations that leave two labelled Routes, or a Route for a host the operator's Route holds, are refused at
render. The central chart's Application no longer ignores spec.hostname, so a host name change reaches the Keycloak.
Names are examples."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT  # noqa: E402

CHART = os.path.join(ROOT, "bootstrap", "helm", "charts", "rhbk")
CENTRAL = os.path.join(ROOT, "bootstrap", "helm", "central")
BASE = {"namespace": "kc-ns", "instanceName": "kc"}


def render(values=None, chart=CHART):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(dict(BASE, **(values or {})), f)
    try:
        p = subprocess.run(["helm", "template", "kc", chart, "-f", f.name], capture_output=True, text=True,
                           timeout=120)
    finally:
        os.unlink(f.name)
    docs = [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)] if p.returncode == 0 else []
    return p, docs


def kinds(docs, kind):
    return [d for d in docs if d.get("kind") == kind]


def wave(doc):
    return int(doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])


def kc(hostname="auth.example.org", ingress=None, routes=None, **more):
    k = {"hostname": hostname}
    if ingress is not None:
        k["ingress"] = {"enabled": ingress}
    if routes is not None:
        k["routes"] = routes
    k.update(more)
    return {"keycloak": k}


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class Defaults(unittest.TestCase):
    def test_without_routes_nothing_new_is_rendered(self):
        p, docs = render(kc())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(kinds(docs, "Route") + kinds(docs, "Certificate") + kinds(docs, "Role"), [])
        cr = kinds(docs, "Keycloak")[0]
        self.assertNotIn("ingress", cr["spec"])     # the operator's default: its own Ingress
        self.assertEqual(cr["spec"]["hostname"], {"hostname": "auth.example.org"})

    def test_ingress_off(self):
        p, docs = render(kc(ingress=False))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(kinds(docs, "Keycloak")[0]["spec"]["ingress"], {"enabled": False})
        p, docs = render(kc(ingress=True))
        self.assertNotIn("ingress", kinds(docs, "Keycloak")[0]["spec"])

    def test_no_host_name_in_the_defaults(self):
        with open(os.path.join(CHART, "values.yaml")) as f:
            values = yaml.safe_load(f)
        self.assertEqual(values["keycloak"]["hostname"], "")
        self.assertEqual(values["keycloak"]["routes"], [])
        self.assertTrue(values["keycloak"]["ingress"]["enabled"])


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class Routes(unittest.TestCase):
    ROUTES = [{"name": "kc-auth", "host": "auth.example.org", "primary": True,
               "certificate": {"clusterIssuer": "public-acme"}},
              {"name": "kc-legacy", "host": "kc.apps.example.org"}]

    def test_the_switch_shape(self):
        p, docs = render(kc(ingress=False, routes=self.ROUTES))
        self.assertEqual(p.returncode, 0, p.stderr)
        routes = {r["metadata"]["name"]: r for r in kinds(docs, "Route")}
        self.assertEqual(set(routes), {"kc-auth", "kc-legacy"})
        labelled = [n for n, r in routes.items() if (r["metadata"].get("labels") or {}).get("app") == "keycloak"]
        self.assertEqual(labelled, ["kc-auth"])
        auth, legacy = routes["kc-auth"]["spec"], routes["kc-legacy"]["spec"]
        self.assertEqual(auth["host"], "auth.example.org")
        self.assertEqual((auth["to"], auth["port"]), ({"kind": "Service", "name": "kc-service"}, {"targetPort": "http"}))
        self.assertEqual(auth["tls"], {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect",
                                       "externalCertificate": {"name": "kc-auth-tls"}})
        self.assertEqual(legacy["tls"], {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"})
        cert = kinds(docs, "Certificate")
        self.assertEqual(len(cert), 1)
        self.assertEqual(cert[0]["spec"]["secretName"], "kc-auth-tls")
        self.assertEqual(cert[0]["spec"]["dnsNames"], ["auth.example.org"])
        self.assertEqual(cert[0]["spec"]["issuerRef"], {"kind": "ClusterIssuer", "name": "public-acme"})
        self.assertEqual(cert[0]["spec"]["privateKey"], {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"})
        role, binding = kinds(docs, "Role")[0], kinds(docs, "RoleBinding")[0]
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["kc-auth-tls"],
                                          "verbs": ["get", "list", "watch"]}])
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "router", "namespace": "openshift-ingress"}])
        self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])
        for d in docs:
            if d["kind"] in ("Route", "Certificate", "Role", "RoleBinding"):
                self.assertEqual(d["metadata"]["namespace"], "kc-ns")
        # the Keycloak first, then the certificate and the router's access, then the routes
        cr = kinds(docs, "Keycloak")[0]
        self.assertLess(wave(cr), wave(cert[0]))
        self.assertEqual(wave(cert[0]), wave(role))
        self.assertLess(wave(cert[0]), min(wave(r) for r in routes.values()))
        self.assertEqual(cr["spec"]["ingress"], {"enabled": False})

    def test_a_second_route_next_to_the_operators_ingress(self):
        """Before a switch: the operator's Ingress keeps the host name and the label; the chart adds a Route for
        another host with its own certificate and no label, and the Keycloak is rendered as without it."""
        _, before = render(kc(hostname="kc.apps.example.org"))
        p, docs = render(kc(hostname="kc.apps.example.org", ingress=True,
                            routes=[{"name": "kc-auth", "host": "auth.example.org",
                                     "certificate": {"clusterIssuer": "public-acme"}}]))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(kinds(docs, "Keycloak"), kinds(before, "Keycloak"))
        route = kinds(docs, "Route")[0]
        self.assertNotIn("labels", route["metadata"])
        self.assertEqual(route["spec"]["tls"]["externalCertificate"], {"name": "kc-auth-tls"})

    def test_names_secrets_and_key_settings(self):
        p, docs = render(kc(ingress=False, routes=[
            {"host": "auth.example.org", "primary": True,
             "certificate": {"clusterIssuer": "ca", "secretName": "auth-cert", "privateKey": {"algorithm": "RSA",
                                                                                              "size": 3072}}},
            {"host": "b.example.org", "certificate": {"clusterIssuer": "ca"}}]))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([r["metadata"]["name"] for r in kinds(docs, "Route")], ["kc-route-0", "kc-route-1"])
        certs = {c["metadata"]["name"]: c["spec"] for c in kinds(docs, "Certificate")}
        self.assertEqual(certs["kc-route-0"]["secretName"], "auth-cert")
        self.assertEqual(certs["kc-route-0"]["privateKey"]["algorithm"], "RSA")
        self.assertEqual(certs["kc-route-0"]["privateKey"]["size"], 3072)
        self.assertEqual(certs["kc-route-1"]["secretName"], "kc-route-1-tls")
        self.assertEqual(kinds(docs, "Role")[0]["rules"][0]["resourceNames"], ["auth-cert", "kc-route-1-tls"])

    def test_no_certificate_no_router_access(self):
        p, docs = render(kc(ingress=False, routes=[{"host": "auth.example.org", "primary": True}]))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(kinds(docs, "Role") + kinds(docs, "Certificate"), [])
        self.assertNotIn("externalCertificate", kinds(docs, "Route")[0]["spec"]["tls"])

    def test_refused(self):
        cases = {
            "keycloak.routes[0].host is required": kc(ingress=False, routes=[{"primary": True}]),
            "host auth.example.org is named twice": kc(ingress=False, routes=[{"host": "auth.example.org"},
                                                                            {"host": "auth.example.org"}]),
            "route name x is used twice": kc(ingress=False, routes=[{"name": "x", "host": "a.example.org"},
                                                                    {"name": "x", "host": "b.example.org"}]),
            # the operator's Route holds keycloak.hostname while its Ingress is on
            "host auth.example.org is keycloak.hostname": kc(routes=[{"host": "auth.example.org"}]),
            # the operator's Route carries app=keycloak while its Ingress is on: two labelled Routes
            "is primary while keycloak.ingress.enabled is true": kc(routes=[{"host": "b.example.org", "primary": True}]),
            "more than one route is primary": kc(hostname="", ingress=False,
                                                 routes=[{"host": "a.example.org", "primary": True},
                                                         {"host": "b.example.org", "primary": True}]),
            "the primary route's host b.example.org is not keycloak.hostname": kc(
                ingress=False, routes=[{"host": "b.example.org", "primary": True}]),
            "needs keycloak.tls.enabled false": kc(ingress=False, routes=[{"host": "auth.example.org"}],
                                                   tls={"enabled": True, "secretName": "t"}),
            "certificate.clusterIssuer is required": kc(ingress=False, routes=[
                {"host": "auth.example.org", "certificate": {"secretName": "s"}}]),
        }
        for text, values in cases.items():
            with self.subTest(text):
                p, _ = render(values)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(text, p.stderr)


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class CentralApplication(unittest.TestCase):
    def application(self, name):
        p = subprocess.run(["helm", "template", "central", CENTRAL], capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return next(d for d in yaml.safe_load_all(p.stdout)
                    if isinstance(d, dict) and d.get("kind") == "Application" and d["metadata"]["name"] == name)

    def test_the_tenant_keycloaks_host_name_is_applied(self):
        app = self.application("rhbk-services")
        pointers = [p for d in app["spec"].get("ignoreDifferences", []) if d.get("kind") == "Keycloak"
                    for p in d.get("jsonPointers", [])]
        self.assertNotIn("/spec/hostname", pointers)
        self.assertIn("RespectIgnoreDifferences=true", app["spec"]["syncPolicy"]["syncOptions"])
        values = yaml.safe_load(app["spec"]["source"]["helm"]["values"])
        self.assertTrue(values["keycloak"]["hostname"])


if __name__ == "__main__":
    unittest.main()
