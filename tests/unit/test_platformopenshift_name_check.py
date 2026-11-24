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
