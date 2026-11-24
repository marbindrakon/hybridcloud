"""A cluster's names are checked first, before the build name is claimed or anything is made.

The real platformopenshift_provision/tasks/cluster_name_check.yml (with common/tasks/name_check.yml and the names
function, and the real preflight_stop.yml) runs in ansible-playbook (po_harness.py). A refused name stops the job
with reason NameNotAllowed and the sentence for the person; an accepted one goes on. The rule: a letter first, then
lower-case letters, digits and single hyphens, a letter or digit last, at most 30 characters; on Google Cloud also
the installer's rule (pkg/validate/validate.go GCPClusterName: no "goog" at the start, no "google" look-alike)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402

ROLE = os.path.join(H.ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision")


def run(name, kind="openstack", tenant="acme-corp", env="prod", dump=False):
    extra = {"ansible_operator_meta": {"name": name, "namespace": "entity-" + tenant}, "resolved_entity": tenant,
             "platform_type": kind, "platform_spec": {"type": kind, kind: {"environment": env}}}
    rc, trace, out = H.run("platformopenshift_provision", "cluster_name_check.yml", extra,
                           result="{'reason': po_fail_reason | default(''), "
                                  "'message': (po_preflight_condition | default({})).message | default('')}",
                           dump_vars=dump)
    return rc, trace, out


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class NameCheck(unittest.TestCase):
    def assert_refused(self, name, words, **kw):
        rc, trace, out = run(name, dump=True, **kw)
        self.assertNotEqual(rc, 0, out)
        facts = json.loads(H.LAST_VARS)
        self.assertEqual(facts["po_fail_reason"], "NameNotAllowed")
        self.assertIn(words, facts["po_preflight_condition"]["message"])
        # nothing written or made before the refusal (a refusal fails the job; the role's rescue writes it)
        self.assertEqual([t for t in trace if "patch" in t or "k8s" in t], [])

    def test_accepted(self):
        for name, kind in (("web", "openstack"), ("web-1", "aws"), ("a" * 30, "gcp"), ("goo-web", "gcp"),
                           ("google-web", "openstack")):
            rc, trace, out = run(name, kind)
            self.assertEqual(rc, 0, (name, kind, out))
            self.assertEqual(trace[-1]["result"], {"reason": "", "message": ""})

    def test_the_cluster_name(self):
        self.assert_refused("a" * 31, "at most 30 characters")
        self.assert_refused("1web", "start with a letter")
        self.assert_refused("web.prod", "The cluster name 'web.prod' is not allowed")
        self.assert_refused("Web", "The cluster name 'Web' is not allowed")
        self.assert_refused("web--a", "single hyphens")

    def test_the_tenant_and_the_environment(self):
        self.assert_refused("web", "The tenant name 'acme.corp' is not allowed", tenant="acme.corp")
        self.assert_refused("web", "The environment name 'prod_1' is not allowed", env="prod_1")

    def test_google_words_on_google_cloud_only(self):
        for name in ("google-web", "googweb", "web-g00gle", "my-go0gle"):
            self.assert_refused(name, "is not allowed on Google Cloud", kind="gcp")
            rc, _, out = run(name, kind="aws")
            self.assertEqual(rc, 0, out)


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class CloudIdentityName(unittest.TestCase):
    """cloud_identity_refusal.yml after the AWS (sts-up) and Google Cloud (wif-up) identity steps, called soft: the
    helper's NameTakenOnSite (an object of the wanted name without the platform's mark) becomes the cluster's reason;
    the helper's sentence, which names the cloud's object, stays in the job's log; any other failure is the step's
    sentence; no failure goes on."""

    def run_r(self, error, result):
        extra = {"ansible_operator_meta": {"name": "web", "namespace": "entity-acme-corp"}, "cir_error": error,
                 "cir_result": result, "cir_cloud": "Google Cloud",
                 "shc_step": "Prepare the cluster's identity on Google Cloud"}
        rc, trace, out = H.run("platformopenshift_provision", "cloud_identity_refusal.yml", extra, dump_vars=True,
                               result="'went on'")
        return rc, trace, json.loads(H.LAST_VARS), out

    def test_states(self):
        taken = "Service account shc-web-x already exists in the project and was not made by the platform"
        rc, _, facts, out = self.run_r(taken, {"ok": False, "error": taken, "reason": "NameTakenOnSite"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(facts["po_fail_reason"], "NameTakenOnSite")
        self.assertNotIn("shc-web-x", facts["po_preflight_condition"]["message"])
        self.assertIn("was not made by the platform for this cluster", facts["po_preflight_condition"]["message"])
        self.assertIn("shc-web-x", out)                        # the job's log
        rc, _, facts, out = self.run_r("HTTP 500", {"ok": False, "error": "HTTP 500"})
        self.assertNotEqual(rc, 0)
        self.assertNotIn("po_fail_reason", facts)
        self.assertIn("Prepare the cluster's identity on Google Cloud", out)
        rc, trace, _, out = self.run_r("", {"ok": True})
        self.assertEqual((rc, trace[-1]), (0, {"result": "went on"}), out)

    def test_both_identity_steps_are_soft_and_checked(self):
        for f, call in (("gcp_clusterbuild_wif.yml", "wif-up"), ("aws_clusterbuild_sts.yml", "sts-up")):
            with open(os.path.join(ROLE, "tasks", f)) as fh:
                text = fh.read()
            i = text.index("[" + call)
            self.assertIn("_soft: true", text[i - 300:i], f)
            self.assertIn("cloud_identity_refusal.yml", text[i:i + 600], f)


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class IssuerLabelKeys(unittest.TestCase):
    """The static issuer's label keys are the product's (hybridsovereign.redhat/oidc-static, oidc-name); for one
    release the jobs also write the older keys (the issuer's sync selects either); the teardown looks for the build's
    ConfigMap under the product key and under the older one, and removes it once."""

    def test_jobs_write_the_product_keys(self):
        for f in ("aws_clusterbuild_sts.yml", "gcp_clusterbuild_wif.yml"):
            with open(os.path.join(ROLE, "tasks", f)) as fh:
                text = fh.read()
            self.assertIn('hybridsovereign.redhat/oidc-static: "true"', text, f)
            self.assertIn("hybridsovereign.redhat/oidc-name:", text, f)
            # the older keys only as the labels of the release before, nowhere else
            self.assertEqual(text.count("shc.signal9.gg"), 2, f)
            self.assertIn('shc.signal9.gg/oidc-static: "true"', text, f)
            self.assertIn("shc.signal9.gg/oidc-name:", text, f)

    def test_teardown_looks_for_both_keys(self):
        cm = {"metadata": {"name": "oidc-static-shc-gcp-web-acme-0a1b2c3d", "namespace": "issuers"}}
        extra = {"central_api_host": "https://hub.example.org", "central_bearer_token": "central-token-not-in-data",
                 "po_build_name": "shc-gcp-web-acme-0a1b2c3d"}
        rc, trace, out = H.run("platformopenshift_teardown", "unpublish_issuer.yml", extra,
                               k8s={"ConfigMap//": [cm]}, result="1")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(H.ROOT, "eda", "rulebooks", "roles", "platformopenshift_teardown", "tasks",
                               "unpublish_issuer.yml")) as fh:
            task = H.yaml.safe_load(fh)[0]
        self.assertEqual(task["loop"], ["hybridsovereign.redhat/oidc-name", "shc.signal9.gg/oidc-name"])
        self.assertEqual(len([t for t in trace if "k8s_info" in t]), 2)
        self.assertEqual([t["k8s"]["name"] for t in trace if "k8s" in t], [cm["metadata"]["name"]])


class Wiring(unittest.TestCase):
    def test_first_step_of_the_job(self):
        with open(os.path.join(ROLE, "tasks", "main.yml")) as f:
            block = H.yaml.safe_load(f)[0]["block"]
        names = [t.get("name") for t in block]
        check = names.index("Check the names before anything is made")
        self.assertLess(names.index("Set platform type from CR"), check)
        for later in ("Read what the hub holds of an installed cluster's build", "Record that the installation is being prepared",
                      "Dispatch to AWS handler", "Dispatch to GCP handler", "Dispatch to OpenStack handler"):
            self.assertLess(check, names.index(later), later)

    def test_the_later_pattern_asserts_are_gone(self):
        for f in ("aws_clusterbuild_facts.yml", "gcp_clusterbuild_facts.yml"):
            with open(os.path.join(ROLE, "tasks", f)) as fh:
                self.assertNotIn("Refuse a build name that can't serve", fh.read(), f)


if __name__ == "__main__":
    unittest.main()
