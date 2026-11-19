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


class NoCutOfAFormatsArguments(unittest.TestCase):
    """`"%s %s" % (a, b)[:N]` cuts the argument tuple, not the text (Python's precedence): no helper or module of the
    platform's jobs has that shape."""

    SHAPE = re.compile(r"%\s*\((?:[^()]|\([^()]*\))*\)\[\s*:")

    def test_the_shape_is_found(self):
        self.assertTrue(self.SHAPE.search('x = "%s %s/%s" % (a, b["n"], c)[:100]'))
        self.assertTrue(self.SHAPE.search('"%s: %s" % (n["name"], f["message"])[:1000]'))
        self.assertFalse(self.SHAPE.search('("%s: %s" % (n["name"], f["message"]))[:1000]'))

    def test_none_in_the_jobs_code(self):
        eda = os.path.join(os.path.dirname(__file__), "..", "..", "eda")
        found = []
        for root, _dirs, files in os.walk(eda):
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(root, f)
                    with open(path, encoding="utf-8") as fh:
                        for i, line in enumerate(fh, 1):
                            if self.SHAPE.search(line):
                                found.append("%s:%d" % (os.path.relpath(path, eda), i))
        self.assertEqual(found, [])


class _Recorder:
    """g.http of the Google Cloud helper: GETs answer 404 (nothing exists), POSTs are recorded."""

    def __init__(self):
        self.posts = []

    def http(self, method, url, body=None, token=None, ok404=False):
        if method == "GET":
            return None
        self.posts.append((url, body))
        return {}


class GoogleCloudRequests(unittest.TestCase):
    """What the helper sends: the service account's and the custom role's names as the API receives them (the old
    cut applied to the format's arguments and sent 104 characters)."""

    def cluster(self, build):
        c = object.__new__(G.Cluster)
        c.cluster, c.project, c.tok, c.changed = build, "example-project", "token-not-in-data", []
        return c

    def test_the_bodies_fit(self):
        rec = _Recorder()
        old_http, old_sleep = G.g.http, G.time.sleep
        G.g.http, G.time.sleep = rec.http, (lambda *_: None)
        try:
            for build, ns, name in INPUTS:
                c = self.cluster(build)
                c.ensure_sa(G.sa_account_id(build, ns, name), {"namespace": ns, "name": name})
                c.custom_role_up(G.role_id(build, ns, name), ["compute.instances.get"],
                                 G.labelled_name(build, ns, name, G.ROLE_TITLE_MAX))
        finally:
            G.g.http, G.time.sleep = old_http, old_sleep
        sas = [b for u, b in rec.posts if u.endswith("/serviceAccounts")]
        roles = [b for u, b in rec.posts if u.endswith("/roles")]
        self.assertEqual((len(sas), len(roles)), (len(INPUTS), len(INPUTS)))
        for b in sas:
            self.assertLessEqual(len(b["serviceAccount"]["displayName"]), 100, b)
            self.assertRegex(b["accountId"], r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
        for b in roles:
            self.assertLessEqual(len(b["role"]["title"]), 100, b)
            self.assertRegex(b["roleId"], r"^[a-zA-Z0-9_.]{3,64}$")


class NothingOfAnotherCluster(unittest.TestCase):
    """A delete or a change selects by an exact id or name: a cluster whose build or cluster name starts another's
    never takes the other's objects."""

    def test_custom_roles_of_a_build_that_starts_another(self):
        short, longer = "shc-gcp-acme-prod-0a1b2c3d", "shc-gcp-acme-prod-0a1b2c3d-east"
        mine = {"name": "projects/p/roles/" + G.role_id(short, "openshift-ingress-operator", "cloud-credentials"),
                "title": G.labelled_name(short, "openshift-ingress-operator", "cloud-credentials", 100)}
        theirs = {"name": "projects/p/roles/" + G.role_id(longer, "openshift-ingress-operator", "cloud-credentials"),
                  "title": G.labelled_name(longer, "openshift-ingress-operator", "cloud-credentials", 100)}
        self.assertTrue(theirs["name"].rsplit("/", 1)[1].startswith("shc_%s_" % short.replace("-", "_")),
                        "the old prefix match took it")
        self.assertTrue(G.owns_role(short, mine))
        self.assertFalse(G.owns_role(short, theirs))
        self.assertTrue(G.owns_role(longer, theirs))
        self.assertFalse(G.owns_role(longer, mine))

    def test_aws_load_balancers_of_a_name_that_starts_another(self):
        def lb(arn, name):
            return {"LoadBalancerArn": arn, "LoadBalancerName": name, "State": {"Code": "active"}}
        lbs = [lb("a1", "prod-ab12c-int"), lb("a2", "a2"), lb("b1", "prod-east-xy34z-int"), lb("b2", "b2")]
        tags = {"a1": {"kubernetes.io/cluster/prod-ab12c": "owned"},
                "a2": {"kubernetes.io/cluster/prod-ab12c": "owned",
                       "kubernetes.io/service-name": "openshift-ingress/router-default"},
                "b1": {"kubernetes.io/cluster/prod-east-xy34z": "owned"},
                "b2": {"kubernetes.io/cluster/prod-east-xy34z": "owned",
                       "kubernetes.io/service-name": "openshift-ingress/router-default"}}
        got = A.select_lbs(lbs, tags, "prod")
        self.assertEqual({k: v["LoadBalancerArn"] for k, v in got.items()}, {"api": "a1", "apps": "a2"})
        got = A.select_lbs(lbs, tags, "prod-east")
        self.assertEqual({k: v["LoadBalancerArn"] for k, v in got.items()}, {"api": "b1", "apps": "b2"})

    def test_aws_two_clusters_of_one_form_are_refused(self):
        lbs = [{"LoadBalancerArn": a, "LoadBalancerName": a + "-int", "State": {"Code": "active"}} for a in ("x", "y")]
        tags = {"x": {"kubernetes.io/cluster/prod-ab12c": "owned"}, "y": {"kubernetes.io/cluster/prod-zz99z": "owned"}}
        with self.assertRaises(SystemExit):
            A.select_lbs(lbs, tags, "prod")

    def test_gcp_forwarding_rules_of_a_name_that_starts_another(self):
        def rule(name, desc=""):
            return {"name": name, "loadBalancingScheme": "INTERNAL", "IPAddress": "192.0.2.%d" % (len(name) % 200),
                    "network": "projects/p/global/networks/net-a", "description": desc}
        ingress = rule("a0123", '{"kubernetes.io/service-name":"openshift-ingress/router-default"}')
        one = [rule("prod-ab12c-api-internal"), ingress]
        self.assertEqual(set(G.select_rules(one, "prod", "net-a")), {"api", "apps"})
        two = [rule("prod-east-xy34z-api-internal"), ingress]
        self.assertEqual(G.select_rules(two, "prod", "net-a"), {}, "another cluster's rules are not prod's")
        both = [rule("prod-ab12c-api-internal"), rule("prod-east-xy34z-api-internal"), ingress]
        got = G.select_rules(both, "prod", "net-a")
        self.assertEqual(got["api"]["name"], "prod-ab12c-api-internal")
        self.assertNotIn("apps", got, "an ingress rule names no cluster: not taken beside another cluster")

    def test_aws_public_subnets_exactly(self):
        sys.path.insert(0, FILES)
        import shc_aws_tenant as T
        name = "shc-ns-p"
        self.assertTrue(T.public_subnet_of(name, "shc-ns-p-public-a", {"AvailabilityZone": "us-east-2a"}))
        self.assertFalse(T.public_subnet_of(name, "shc-ns-p-public-public-a", {"AvailabilityZone": "us-east-2a"}))
        self.assertFalse(T.public_subnet_of(name, "shc-ns-p-public-b", {"AvailabilityZone": "us-east-2a"}))


if __name__ == "__main__":
    unittest.main()
