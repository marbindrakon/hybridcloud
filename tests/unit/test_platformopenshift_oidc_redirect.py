"""The sign-in client of a tenant-built OpenShift cluster accepts only that cluster's OAuth callback (H2GAS-F2).

eda/rulebooks/roles/platformopenshift_provision/tasks/keycloak_oidc.yml registers one exact redirect URI,
https://oauth-openshift.apps.<cluster domain>/oauth2callback/<identity provider name>, built from the cluster's domain
that each build path (OpenStack, AWS, GCP) passes; the identity provider of the cluster's OAuth has the same name. An
existing client with other redirect URIs is converged in place. No wildcard, no console path.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision")
OIDC = os.path.join(ROLE, "tasks", "keycloak_oidc.yml")
DEFAULTS = os.path.join(ROLE, "defaults", "main.yml")
SET = "Set the cluster's OAuth callback for the sign-in client"
REFUSE = "Refuse a sign-in callback without the cluster's domain"
CONVERGE = "Converge the sign-in callback of an existing OIDC client"
INCLUDE_NAME = "Configure Keycloak OIDC client and ACM OAuth policy for this cluster"


def render(**extra):
    tasks = load(OIDC)
    play = [by_name(tasks, SET), by_name(tasks, REFUSE), write_out("ocp_oidc_redirect_uris")]
    base = {"skip_oidc_setup": False, "ocp_oidc_client_id": "c1-keycloak-oidc",
            "platformopenshift_oidc_idp_name": load(DEFAULTS)["platformopenshift_oidc_idp_name"]}
    base.update(extra)
    return run_play(play, base)


class Wiring(unittest.TestCase):
    def test_every_build_path_passes_the_cluster_domain(self):
        want = {"openstack.yml": "{{ cb_cluster_name }}.{{ cb_cluster_domain }}",
                "aws.yml": "{{ cb_cluster_domain }}",
                "gcp.yml": "{{ cb_cluster_domain }}"}
        for f, dom in want.items():
            inc = by_name(load(os.path.join(ROLE, "tasks", f)), INCLUDE_NAME)
            self.assertEqual(inc["ansible.builtin.include_tasks"], "keycloak_oidc.yml", f)
            self.assertEqual(inc["vars"]["cluster_domain"], dom, f)

    def test_client_registers_only_the_rendered_callback(self):
        tasks = load(OIDC)
        create = by_name(tasks, "Create OIDC client in Keycloak for OpenShift cluster")
        self.assertEqual(create["ansible.builtin.uri"]["body"]["redirectUris"], "{{ ocp_oidc_redirect_uris }}")
        conv = by_name(tasks, CONVERGE)
        self.assertEqual(conv["ansible.builtin.uri"]["method"], "PUT")
        self.assertIn("combine({'redirectUris': ocp_oidc_redirect_uris})", conv["ansible.builtin.uri"]["body"])
        self.assertTrue(conv["no_log"])
        names = [t.get("name") for t in walk(tasks)]
        self.assertLess(names.index(REFUSE), names.index("Create OIDC client in Keycloak for OpenShift cluster"))
        self.assertLess(names.index("Get OIDC client UUID"), names.index(CONVERGE))

    def test_identity_provider_name_matches_the_callback(self):
        policy = by_name(load(OIDC), "Build ACM Policy for OpenShift OAuth configuration")
        tpl = policy["ansible.builtin.set_fact"]["acm_oidc_policy"]["spec"]["policy-templates"]
        oauth = [t for t in tpl if t["objectDefinition"]["metadata"]["name"] == "{{ cluster_name }}-oauth"][0]
        idp = oauth["objectDefinition"]["spec"]["object-templates"][0]["objectDefinition"]["spec"]["identityProviders"][0]
        self.assertEqual(idp["name"], "{{ platformopenshift_oidc_idp_name }}")
        self.assertEqual(load(DEFAULTS)["platformopenshift_oidc_idp_name"], "keycloak-sso")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Rendered(unittest.TestCase):
    def test_openstack_cluster(self):
        self.assertEqual(render(cluster_domain="g-ocp-1.g-oso-1.acme.tenants.example.org",
                                cluster_api_url="https://api.g-ocp-1.g-oso-1.acme.tenants.example.org:6443"),
                         ["https://oauth-openshift.apps.g-ocp-1.g-oso-1.acme.tenants.example.org"
                          "/oauth2callback/keycloak-sso"])

    def test_aws_and_gcp_cluster(self):
        self.assertEqual(render(cluster_domain="web1.aws-env.acme.example.net"),
                         ["https://oauth-openshift.apps.web1.aws-env.acme.example.net/oauth2callback/keycloak-sso"])

    def test_domain_from_api_url_when_not_passed(self):
        self.assertEqual(render(cluster_domain="", cluster_api_url="https://api.c2.example.net:6443"),
                         ["https://oauth-openshift.apps.c2.example.net/oauth2callback/keycloak-sso"])

    def test_no_wildcard_and_no_console_path(self):
        uris = render(cluster_domain="C3.Example.NET", cluster_console_url="https://console-openshift-console.apps.c3.example.net")
        self.assertEqual(uris, ["https://oauth-openshift.apps.c3.example.net/oauth2callback/keycloak-sso"])
        self.assertFalse([u for u in uris if "*" in u or "console-openshift-console" in u])

    def test_unknown_domain_is_refused(self):
        with self.assertRaises(AssertionError) as ctx:
            render(cluster_domain="", cluster_api_url="")
        self.assertIn("domain is unknown", str(ctx.exception))
        with self.assertRaises(AssertionError):
            render(cluster_domain="*.apps.c4.example.net")


if __name__ == "__main__":
    unittest.main()
