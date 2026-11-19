"""The names the cloud helpers build for an OpenShift cluster fit their provider's limits at the longest inputs the
platform allows: a build name of 40 characters (po_preflight.py build-name with max 40 for AWS and Google Cloud), a
CredentialsRequest's secret namespace of 63 characters (a namespace is a DNS label) and secret name of 253 (a
Kubernetes object name), and the shortest ones.

Example names only.
"""
import os
import re
import sys
import unittest

FILES = os.path.join(os.path.dirname(__file__), "..", "..", "eda", "common", "files")
sys.path.insert(0, FILES)
import shc_aws_cluster as A  # noqa: E402
import shc_gcp_cluster as G  # noqa: E402

BUILD_MAX = "shc-gcp-" + "a" * 23 + "-0a1b2c3d"          # 40 characters
BUILD_SHORT = "shc-gcp-a-b-0a1b2c3d"
NS_MAX = "n" * 63
NAME_MAX = "s" * 253
# the shape of the request that failed: a build name and a namespace/name of more than 100 characters together
GATE = ("shc-gcp-example-u2g1-1b4b4ba7", "openshift-cloud-network-config-controller", "cloud-credentials-example-abcdef")

INPUTS = [(BUILD_MAX, NS_MAX, NAME_MAX), (BUILD_MAX, "openshift-ingress-operator", "cloud-credentials"),
          (BUILD_SHORT, "a", "b"), GATE]


class GoogleCloudNames(unittest.TestCase):
    def test_lengths(self):
        self.assertEqual(len(BUILD_MAX), 40)
        for cluster, ns, name in INPUTS:
            with self.subTest(cluster=cluster, ns=len(ns), name=len(name)):
                pid = G.pool_id(cluster)
                self.assertRegex(pid, r"^[a-z][a-z0-9-]{2,30}[a-z0-9]$")
                self.assertLessEqual(len(pid), 32)
                self.assertFalse(pid.startswith("gcp-"))
                sa = G.sa_account_id(cluster, ns, name)
                self.assertRegex(sa, r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
                rid = G.role_id(cluster, ns, name)
                self.assertRegex(rid, r"^[a-zA-Z0-9_.]{3,64}$")
                for limit in (G.SA_DISPLAY_NAME_MAX, G.ROLE_TITLE_MAX):
                    dn = G.labelled_name(cluster, ns, name, limit)
                    self.assertLessEqual(len(dn), 100)
                    self.assertTrue(dn.startswith(cluster + " "), "the build name stays whole")
                self.assertLessEqual(len(cluster[:G.POOL_DISPLAY_NAME_MAX]), 32)
                self.assertLessEqual(len("OpenShift cluster %s (SHC)" % cluster), 256)   # pool description
                self.assertLessEqual(len("shc-cluster=" + cluster), 256)                  # account description

    def test_the_gates_display_name_is_cut(self):
        """Over 100 characters together; the API takes 100."""
        full = "%s %s/%s" % GATE
        self.assertGreater(len(full), 100)
        dn = G.labelled_name(*GATE, G.SA_DISPLAY_NAME_MAX)
        self.assertEqual(len(dn), 100)
        self.assertEqual(dn, full[:100])

    def test_a_short_name_is_kept_whole(self):
        self.assertEqual(G.labelled_name("shc-gcp-a-b-0a1b2c3d", "ns", "name", 100), "shc-gcp-a-b-0a1b2c3d ns/name")

    def test_distinct_requests_keep_distinct_ids(self):
        a = G.sa_account_id(BUILD_MAX, "openshift-ingress-operator", "cloud-credentials")
        b = G.sa_account_id(BUILD_MAX, "openshift-cloud-network-config-controller", "cloud-credentials")
        self.assertNotEqual(a, b)
        self.assertNotEqual(G.role_id(BUILD_MAX, NS_MAX, NAME_MAX), G.role_id(BUILD_MAX, NS_MAX, NAME_MAX[:-1]))


class AwsNames(unittest.TestCase):
    def test_lengths(self):
        for cluster, ns, name in INPUTS:
            cluster = cluster.replace("shc-gcp-", "shc-aws-")
            with self.subTest(cluster=cluster, ns=len(ns), name=len(name)):
                rn = A.role_name(cluster, ns, name)
                self.assertRegex(rn, r"^[\w+=,.@-]{1,64}$")                       # IAM role name
                self.assertLessEqual(len("/shc/%s/" % cluster), 512)                 # IAM path
                self.assertLessEqual(len("OpenShift %s: %s/%s" % (cluster, ns, name)), 1000)  # role description
                self.assertLessEqual(len(cluster), 256)                              # tag value shc-cluster
                self.assertLessEqual(len("kubernetes.io/cluster/%s-" % cluster[:21]), 128)

    def test_policy_names_of_the_release(self):
        """The inline policy is named after the request's secret: the secrets of OpenShift's AWS CredentialsRequests
        are short; the name limit is 128."""
        for secret in ("cloud-credentials", "ebs-cloud-credentials", "installer-cloud-credentials",
                       "aws-cloud-credentials", "cloud-credential-operator-iam-ro-creds"):
            self.assertLessEqual(len("%s-policy" % secret), 128)
        self.assertTrue(re.match(r"^[\w+=,.@-]{1,128}$", "%s-policy" % NAME_MAX[:120]))


if __name__ == "__main__":
    unittest.main()
