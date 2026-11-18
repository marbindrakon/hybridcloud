"""The installation values of the central chart (bootstrap/helm/central/values.yaml) name one tenant Keycloak.

Judged on the parsed values and on the services Keycloak chart rendered with them: every consumer of the tenant
Keycloak (an issuer or URL of the tenant realm, the jobs' KEYCLOAK_SERVICES_URL and their KEYCLOAK_URL next to the
tenant Keycloak's admin secret or realm, the cluster operator's externalUrl) names the Keycloak's host name; the
rendered chart has exactly one Route with the label app=keycloak when its own routes serve the Keycloak, and none when
the operator's Ingress does (the operator's Route carries it then); and when an OpenStack site declares sign-in, the
tenant issuer is at most 64 characters (an identity service can limit an identity provider's remote ID to that:
Keystone 2026.2 does)."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT  # noqa: E402

VALUES = os.path.join(ROOT, "bootstrap", "helm", "central", "values.yaml")
CHART = os.path.join(ROOT, "bootstrap", "helm", "charts", "rhbk")
REALIZATION = os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml")
ISSUER_MAX = 64


def values():
    with open(VALUES) as f:
        return yaml.safe_load(f)


def tenant_realm():
    with open(REALIZATION) as f:
        real = yaml.safe_load(f)
    return real["fabric_realization_defaults"]["openshift"]["tenantSignIn"]["realm"]


def walk(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, path + (k,))
    elif isinstance(node, list):
        envs = {e.get("name"): e.get("value") for e in node if isinstance(e, dict) and "name" in e and "value" in e}
        if envs:
            yield path, envs
        for i, v in enumerate(node):
            yield from walk(v, path + (i,))
    else:
        yield path, node


class TenantKeycloakValues(unittest.TestCase):
    def setUp(self):
        self.v = values()
        self.kc = self.v["rhbkServices"]["values"]["keycloak"]
        self.host = self.kc["hostname"]
        self.realm = tenant_realm()
        self.url = f"https://{self.host}"
        self.issuer = f"{self.url}/realms/{self.realm}"

    def consumers(self):
        """(where, value) of every place that names the tenant Keycloak."""
        out = []
        realm_url = re.compile(r"https?://[^/\s\"']+/realms/" + re.escape(self.realm) + r"(?=/|$)")
        for path, node in walk(self.v):
            where = ".".join(str(p) for p in path)
            if isinstance(node, str):
                out += [(where, m.group(0)) for m in realm_url.finditer(node)]
            elif isinstance(node, dict):   # an env list
                if node.get("KEYCLOAK_SERVICES_URL"):
                    out.append((where + ".KEYCLOAK_SERVICES_URL", node["KEYCLOAK_SERVICES_URL"]))
                tenant = (str(node.get("KEYCLOAK_ADMIN_SECRET") or "").startswith(self.v["rhbkServices"]["values"]["instanceName"])
                          or node.get("KEYCLOAK_REALM") == self.realm)
                if node.get("KEYCLOAK_URL") and tenant:
                    out.append((where + ".KEYCLOAK_URL", node["KEYCLOAK_URL"]))
        po = (self.v.get("platformOpenshiftOperator") or {}).get("values", {}).get("keycloak") or {}
        if po.get("externalUrl") and po.get("realm") == self.realm:
            out.append(("platformOpenshiftOperator.values.keycloak.externalUrl", po["externalUrl"]))
        return out

    def test_every_consumer_names_the_keycloaks_host_name(self):
        found = self.consumers()
        self.assertGreaterEqual(len(found), 14, found)   # with keycloak.hostname itself: the 15 places
        wrong = [(w, v) for w, v in found if not (v.rstrip("/") == self.url or v.startswith(self.url + "/realms/"))]
        self.assertEqual(wrong, [])
        # the two writers of the services cluster's OAuth issuer agree
        issuers = [v for w, v in found if v.rstrip("/").endswith(f"/realms/{self.realm}")]
        self.assertIn(self.issuer, issuers)
        self.assertEqual(self.v["sovereignNamespacesServices"]["values"]["oauth"]["issuer"], self.issuer)

    def test_the_issuer_fits_a_site_that_declares_sign_in(self):
        declared = []
        for path, node in walk(self.v):
            if path and path[-1] == "identityProvider" and "signIn" in path:
                declared.append(".".join(str(p) for p in path))
        if declared:
            self.assertLessEqual(len(self.issuer), ISSUER_MAX,
                                 f"{self.issuer} ({len(self.issuer)} characters) and sign-in declared at {declared}")

    @unittest.skipUnless(shutil.which("helm"), "helm not installed")
    def test_one_labelled_route(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(self.v["rhbkServices"]["values"], f)
        try:
            p = subprocess.run(["helm", "template", "rhbk-services", CHART, "-f", f.name], capture_output=True,
                               text=True, timeout=120)
        finally:
            os.unlink(f.name)
        self.assertEqual(p.returncode, 0, p.stderr)
        docs = [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)]
        routes = [d for d in docs if d["kind"] == "Route"]
        labelled = [r["spec"]["host"] for r in routes if (r["metadata"].get("labels") or {}).get("app") == "keycloak"]
        cr = next(d for d in docs if d["kind"] == "Keycloak")
        if (cr["spec"].get("ingress") or {}).get("enabled", True):
            self.assertEqual(labelled, [])       # the operator's Route is the labelled one
        else:
            self.assertEqual(labelled, [self.host])
        # the services cluster's consumers pin the ingress CA bundle, which holds the ECDSA chain only
        for c in (d for d in docs if d["kind"] == "Certificate"):
            self.assertEqual(c["spec"]["privateKey"]["algorithm"], "ECDSA")


if __name__ == "__main__":
    unittest.main()
