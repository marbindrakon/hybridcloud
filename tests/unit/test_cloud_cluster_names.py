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

    # ---- the call sites (a prefix put back fails these) -----------------------------------------------------------
    def test_wif_down_deletes_only_its_own_roles(self):
        short, longer = "shc-gcp-acme-prod-0a1b2c3d", "shc-gcp-acme-prod-0a1b2c3d-east"
        roles = [{"name": "projects/p/roles/" + G.role_id(b, "openshift-ingress-operator", "cloud-credentials"),
                  "title": G.labelled_name(b, "openshift-ingress-operator", "cloud-credentials", 100)}
                 for b in (longer, short)]
        rec = _Recorder()
        old = (G.g.http, G.g.list_all)
        G.g.http = rec.http
        G.g.list_all = lambda url, key, tok, params=None: roles if url.endswith("/roles") else []
        try:
            c = object.__new__(G.Cluster)
            c.cluster, c.project, c.tok, c.changed = short, "p", "token-not-in-data", []
            c.pool = "https://iam.example.invalid/pool"
            c.pool_id = "pool"
            c.down()
        finally:
            G.g.http, G.g.list_all = old
        self.assertEqual(c.changed, ["removed custom role " + roles[1]["name"].rsplit("/", 1)[1]])

    def test_the_iam_role_list_is_exactly_the_clusters_path(self):
        class Pager:
            def paginate(self, PathPrefix):
                return [{"Roles": [{"RoleName": "a", "Path": "/shc/prod/"}, {"RoleName": "b", "Path": "/shc/prod/x/"}]}]

        class Iam:
            def get_paginator(self, name):
                return Pager()
        c = object.__new__(A.Cluster)
        c.iam, c.path = Iam(), "/shc/prod/"
        self.assertEqual([r["RoleName"] for r in c.roles()], ["a"])

    def test_place_public_down_deletes_only_its_subnets(self):
        sys.path.insert(0, FILES)
        import shc_aws_tenant as T

        class Ec2:
            def __init__(self):
                self.deleted = []

            def describe_vpcs(self, Filters):
                return {"Vpcs": [{"VpcId": "vpc-1", "Tags": []}]}

            def describe_subnets(self, Filters):
                mk = lambda i, n, az: {"SubnetId": i, "AvailabilityZone": az, "Tags": [{"Key": "Name", "Value": n}]}
                return {"Subnets": [mk("s1", "shc-ns-p-public-a", "us-east-2a"),
                                    mk("s2", "shc-ns-p-public-public-a", "us-east-2a"),
                                    mk("s3", "shc-ns-p-public-b-extra", "us-east-2b")]}

            def describe_route_tables(self, Filters):
                return {"RouteTables": []}

            def describe_internet_gateways(self, Filters):
                return {"InternetGateways": []}

            def delete_subnet(self, SubnetId):
                self.deleted.append(SubnetId)
        t = object.__new__(T.Tenant)
        t.ec2, t.cfg, t.changed = Ec2(), {}, []
        t.place_public("ns/p", "down")
        self.assertEqual(t.ec2.deleted, ["s1"])


# ---- the published names: the cluster's own load balancers by its infrastructure id ------------------------------
class _R53:
    def __init__(self, zones):
        self.zones, self.upserts = zones, []

    def list_hosted_zones_by_name(self, DNSName, MaxItems):
        return {"HostedZones": [{"Id": "/hostedzone/" + z["id"], "Name": z["name"], "Config": {"PrivateZone": z["private"]}}
                                for z in self.zones]}

    def list_tags_for_resource(self, ResourceType, ResourceId):
        z = next(z for z in self.zones if z["id"] == ResourceId)
        return {"ResourceTagSet": {"Tags": [{"Key": k, "Value": v} for k, v in z.get("tags", {}).items()]}}

    def change_resource_record_sets(self, HostedZoneId, ChangeBatch):
        for ch in ChangeBatch["Changes"]:
            self.upserts.append((ch["ResourceRecordSet"]["Name"], ch["ResourceRecordSet"]["AliasTarget"]["DNSName"]))


class _Elb:
    def __init__(self, lbs, tags):
        self.lbs, self.tags = lbs, tags

    def get_paginator(self, name):
        lbs = self.lbs

        class P:
            def paginate(self):
                return [{"LoadBalancers": lbs}]
        return P()

    def describe_tags(self, ResourceArns):
        return {"TagDescriptions": [{"ResourceArn": a, "Tags": [{"Key": k, "Value": v} for k, v in self.tags[a].items()]}
                                    for a in ResourceArns]}


def _lb(arn, name):
    return {"LoadBalancerArn": arn, "LoadBalancerName": name, "State": {"Code": "active"}, "VpcId": "vpc-1",
            "Scheme": "internal", "DNSName": arn + ".elb.example", "CanonicalHostedZoneId": "Z1"}


# two clusters on one network: names sharing their first 21 characters, and a name with "--"
PAIRS = [("averyveryverylongname-a", "averyveryverylongname-abc12", "averyveryverylongname-b", "averyveryverylongname-xyz98"),
         ("prod", "prod-ab12c", "prod-east", "prod-east-xy34z"),
         ("a--b", "a--b-q1w2e", "a--b-c", "a--b-c-r5t6y")]


class PublishedNames(unittest.TestCase):
    def aws(self, name, infra_mine, name2, infra_other, own_zone=True):
        zn = "env.example.org."
        zones = [{"id": "PUB", "name": zn, "private": False},
                 {"id": "Z2", "name": "%s.%s" % (name2, zn), "private": True,
                  "tags": {"kubernetes.io/cluster/" + infra_other: "owned"}}]
        if own_zone:
            zones.append({"id": "Z1", "name": "%s.%s" % (name, zn), "private": True,
                          "tags": {"kubernetes.io/cluster/" + infra_mine: "owned", "Name": "x"}})
        lbs, tags = [], {}
        for infra in (infra_other, infra_mine):
            lbs += [_lb(infra + "-api", infra[:20] + "-int"), _lb(infra + "-apps", "a" + infra[:10])]
            tags[infra + "-api"] = {"kubernetes.io/cluster/" + infra: "owned"}
            tags[infra + "-apps"] = {"kubernetes.io/cluster/" + infra: "owned",
                                     "kubernetes.io/service-name": "openshift-ingress/router-default"}
        r53 = _R53(zones)

        class S:
            def client(self, kind):
                return r53 if kind == "route53" else _Elb(lbs, tags)
        old = A.session
        A.session = lambda cfg: S()
        try:
            out = A.dns_alias({}, "up", "env.example.org", name, "vpc-1", wait=0)
        finally:
            A.session = old
        return out, r53.upserts

    def test_aws_each_cluster_gets_its_own_load_balancers(self):
        for a, ia, b, ib in PAIRS:
            for name, mine, other, iother in ((a, ia, b, ib), (b, ib, a, ia)):
                with self.subTest(name=name):
                    out, ups = self.aws(name, mine, other, iother)
                    self.assertTrue(out["complete"], out)
                    self.assertEqual(out["infraID"], mine)
                    self.assertEqual(sorted(t for _, t in ups), sorted([mine + "-api.elb.example", mine + "-apps.elb.example"]))

    def test_aws_without_its_own_resources_nothing_is_published(self):
        a, ia, b, ib = PAIRS[1]
        out, ups = self.aws(a, ia, b, ib, own_zone=False)
        self.assertEqual((out["complete"], out["missing"], ups), (False, ["api", "apps"], []))

    def gcp(self, name, infra_mine, name2, infra_other, own_zone=True):
        zn = "env.example.org."
        zones = [{"name": "pub", "dnsName": zn, "visibility": "public"},
                 {"name": infra_other + "-private-zone", "dnsName": "%s.%s" % (name2, zn), "visibility": "private"}]
        if own_zone:
            zones.append({"name": infra_mine + "-private-zone", "dnsName": "%s.%s" % (name, zn), "visibility": "private"})
        rules, members = [], {}
        for n, infra in enumerate((infra_other, infra_mine)):
            rules.append({"name": infra + "-api-internal", "loadBalancingScheme": "INTERNAL", "IPAddress": "10.0.0.%d" % (n + 1),
                          "network": "projects/p/global/networks/net-a"})
            rules.append({"name": "a%d" % n, "loadBalancingScheme": "INTERNAL", "IPAddress": "10.0.1.%d" % (n + 1),
                          "network": "projects/p/global/networks/net-a", "backendService": "bs/%d" % n,
                          "description": '{"kubernetes.io/service-name":"openshift-ingress/router-default"}'})
            members["bs/%d" % n] = [infra + "-worker-b-abcd", infra + "-worker-c-efgh"]
        posts = []

        def list_all(url, key, tok, params=None):
            if url.endswith("/managedZones"):
                return zones
            if url.endswith("/rrsets"):
                return []
            return rules

        def http(method, url, body=None, token=None, **kw):
            if method == "GET" and url in members:
                return {"backends": [{"group": "ig/" + url}]}
            if url.endswith("/listInstances"):
                return {"items": [{"instance": "zones/z/instances/" + i} for i in members[url[3:-len("/listInstances")]]]}
            posts.append(body)
            return {}

        class Ident:
            def project(self, p):
                return "token-not-in-data"
        old = (G.g.list_all, G.g.http, G.g.Identity)
        G.g.list_all, G.g.http, G.g.Identity = list_all, http, Ident
        try:
            out = G.dns_alias({"project": "p", "region": "r"}, "up", "env.example.org", name, "net-a", wait=0)
        finally:
            G.g.list_all, G.g.http, G.g.Identity = old
        return out, posts

    def test_gcp_each_cluster_gets_its_own_rules(self):
        for a, ia, b, ib in PAIRS:
            for name, mine, other, iother in ((a, ia, b, ib), (b, ib, a, ia)):
                with self.subTest(name=name):
                    out, posts = self.gcp(name, mine, other, iother)
                    self.assertTrue(out["complete"], out)
                    self.assertEqual(out["infraID"], mine)
                    want = {"10.0.0.2", "10.0.1.2"}   # this cluster's rules come second in the list
                    self.assertEqual({r for p in posts for a2 in p.get("additions", []) for r in a2["rrdatas"]}, want)

    def test_gcp_without_its_own_resources_nothing_is_published(self):
        a, ia, b, ib = PAIRS[0]
        out, posts = self.gcp(a, ia, b, ib, own_zone=False)
        self.assertEqual((out["complete"], posts), (False, []))

    def test_the_infra_ids_are_read_exactly(self):
        zn = "env.example.org."
        self.assertEqual(G.infra_id_of_zones([{"name": "x-private-zone", "dnsName": "prod-east." + zn,
                                              "visibility": "private"}], "prod", zn), None)
        self.assertIsNone(A.infra_id_of_zone("prod-east." + zn, "prod." + zn, {"kubernetes.io/cluster/p-1": "owned"}))
        self.assertIsNone(A.infra_id_of_zone("prod." + zn, "prod." + zn, {"kubernetes.io/cluster/p-1": "owned",
                                                                          "kubernetes.io/cluster/p-2": "owned"}))
        self.assertEqual(A.select_lbs([_lb("x", "x-int")], {"x": {"kubernetes.io/cluster/prod-ab12c-x": "owned"}},
                                      "prod-ab12c"), {}, "a tag that only starts with the id")


if __name__ == "__main__":
    unittest.main()
