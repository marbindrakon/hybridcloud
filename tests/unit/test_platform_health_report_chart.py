"""shc-cluster-base: the writer of the admin dashboard's platform-health banner (ops.platformHealth.report in
templates/platform-health.yaml). Off renders nothing of it; on the services cluster the writer, the index and one
report ConfigMap per reporting cluster (without data, so that a sync keeps what the writers wrote), a writer Role per
cluster (get and patch on its own report, no create), the reader Role by name, and the accounts of the named writers;
on another cluster the writer with the services cluster's Secret and no Role on the services cluster's namespace;
exactly the rights each account uses; missing or wrong settings fail with their names; the script's configuration
carries the values; no lab value in what it renders."""
import json
import os
import re
import shutil
import subprocess
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CHART = os.path.join(ROOT, "charts/charts/shc-cluster-base")
BASE = ["--set", "layers.base=false", "--set", "layers.certs=false", "--set", "layers.ops=true"]
R = "ops.platformHealth.report."
ON = ["--set", R + "enabled=true", "--set", R + "namespace=platform"]
SERVICES = ON + ["--set", R + "cluster=services", "--set", R + "onServicesCluster=true",
                 "--set", R + "readerGroups[0]=platform-admins"]
HUB = ON + ["--set", R + "cluster=hub", "--set", R + "onServicesCluster=false",
            "--set", R + "servicesCluster.secret=services-api", "--set", R + "servicesCluster.serverKey=server",
            "--set", R + "servicesCluster.tokenKey=token"]
LAB = re.compile(r"signal9|172\.18\.|2602:0{0,3}81f:|central-rhbk|services-rhbk|services-quay|shc-central")
MINE = {"platform-health-report", "shc-platform-health-report", "platform-health-report-checks",
        "shc-platform-health", "shc-platform-health-reader"}


def render(*args, ok=True):
    p = subprocess.run(["helm", "template", "t", CHART] + BASE + list(args), capture_output=True, text=True,
                       timeout=120)
    if not ok:
        return p
    if p.returncode != 0:
        raise AssertionError(p.stderr)
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def by_key(docs):
    return {(d["kind"], d["metadata"]["name"], d["metadata"].get("namespace")): d for d in docs}


def mine(docs):
    return {k: d for k, d in by_key(docs).items()
            if k[1] in MINE or k[1].startswith(("platform-health-writer-", "shc-platform-health-"))}


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class Render(unittest.TestCase):
    def test_off_renders_nothing_of_it(self):
        self.assertEqual(mine(render()), {})
        # the switch alone decides: settings without it render nothing
        self.assertEqual(mine(render(*(SERVICES[2:] + ["--set", R + "namespace=platform"]))), {})

    def test_on_the_services_cluster(self):
        docs = render(*(SERVICES + ["--set", R + "writers[0]=hub", "--set", R + "writers[1]=edge"]))
        k = mine(docs)
        self.assertEqual(set(k), {
            ("ServiceAccount", "platform-health-report", "shc-ops"),
            ("RoleBinding", "shc-platform-health-report", "openshift-monitoring"),
            ("Role", "platform-health-report-checks", "shc-ops"),
            ("RoleBinding", "platform-health-report-checks", "shc-ops"),
            ("ConfigMap", "platform-health-report", "shc-ops"),
            ("CronJob", "platform-health-report", "shc-ops"),
            ("ConfigMap", "shc-platform-health", "platform"),
            ("ConfigMap", "shc-platform-health-services", "platform"),
            ("ConfigMap", "shc-platform-health-hub", "platform"),
            ("ConfigMap", "shc-platform-health-edge", "platform"),
            ("Role", "shc-platform-health-writer-services", "platform"),
            ("RoleBinding", "shc-platform-health-writer-services", "platform"),
            ("Role", "shc-platform-health-writer-hub", "platform"),
            ("RoleBinding", "shc-platform-health-writer-hub", "platform"),
            ("Role", "shc-platform-health-writer-edge", "platform"),
            ("RoleBinding", "shc-platform-health-writer-edge", "platform"),
            ("Role", "shc-platform-health-reader", "platform"),
            ("RoleBinding", "shc-platform-health-reader", "platform"),
            ("ServiceAccount", "platform-health-writer-hub", "shc-ops"),
            ("Secret", "platform-health-writer-hub", "shc-ops"),
            ("ServiceAccount", "platform-health-writer-edge", "shc-ops"),
            ("Secret", "platform-health-writer-edge", "shc-ops")})
        # the index lists every reporting cluster, this one first; the reports carry no data (the writers fill them)
        self.assertEqual(json.loads(k[("ConfigMap", "shc-platform-health", "platform")]["data"]["clusters.json"]),
                         ["services", "hub", "edge"])
        for c in ("services", "hub", "edge"):
            self.assertNotIn("data", k[("ConfigMap", "shc-platform-health-" + c, "platform")])
        # each writer: get and patch on its own report only; no create anywhere
        for c, account in (("services", "platform-health-report"), ("hub", "platform-health-writer-hub"),
                           ("edge", "platform-health-writer-edge")):
            self.assertEqual(k[("Role", "shc-platform-health-writer-" + c, "platform")]["rules"], [
                {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["shc-platform-health-" + c],
                 "verbs": ["get", "patch"]}])
            b = k[("RoleBinding", "shc-platform-health-writer-" + c, "platform")]
            self.assertEqual(b["roleRef"]["name"], "shc-platform-health-writer-" + c)
            self.assertEqual([(x["kind"], x["name"], x["namespace"]) for x in b["subjects"]],
                             [("ServiceAccount", account, "shc-ops")])
        for d in docs:
            if d["kind"] == "Role":
                for rule in d["rules"]:
                    self.assertNotIn("create", rule["verbs"], d["metadata"]["name"])
        secret = k[("Secret", "platform-health-writer-hub", "shc-ops")]
        self.assertEqual(secret["type"], "kubernetes.io/service-account-token")
        self.assertEqual(secret["metadata"]["annotations"]["kubernetes.io/service-account.name"],
                         "platform-health-writer-hub")
        self.assertNotIn("data", secret)
        # readers: get on the index and on each report, by name, bound to the named groups
        self.assertEqual(k[("Role", "shc-platform-health-reader", "platform")]["rules"], [
            {"apiGroups": [""], "resources": ["configmaps"],
             "resourceNames": ["shc-platform-health", "shc-platform-health-services", "shc-platform-health-hub",
                               "shc-platform-health-edge"],
             "verbs": ["get"]}])
        self.assertEqual(k[("RoleBinding", "shc-platform-health-reader", "platform")]["subjects"],
                         [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": "platform-admins"}])
        # this cluster's reads: the monitoring's alerts, the check Jobs and pods
        am = k[("RoleBinding", "shc-platform-health-report", "openshift-monitoring")]
        self.assertEqual((am["roleRef"]["kind"], am["roleRef"]["name"]), ("Role", "monitoring-alertmanager-view"))
        self.assertEqual(k[("Role", "platform-health-report-checks", "shc-ops")]["rules"], [
            {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["list"]},
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}])
        cfg = json.loads(k[("ConfigMap", "platform-health-report", "shc-ops")]["data"]["config.json"])
        self.assertEqual((cfg["cluster"], cfg["targetMode"], cfg["targetNamespace"], cfg["targetConfigMap"],
                          cfg["intervalSeconds"], cfg["receiverCheck"], cfg["opsNamespace"], cfg["checkCronJobs"]),
                         ("services", "local", "platform", "shc-platform-health-services", 300, False, "shc-ops",
                          "eda-activations|eda-api-check|identity-write-probe-.+"))
        spec = k[("CronJob", "platform-health-report", "shc-ops")]["spec"]
        self.assertEqual(spec["schedule"], "*/5 * * * *")
        pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "platform-health-report")
        self.assertEqual([v["name"] for v in pod["volumes"]], ["config"])
        c = pod["containers"][0]
        self.assertEqual(c["image"], "registry.redhat.io/ubi9/python-312-minimal:latest")
        self.assertTrue(c["securityContext"]["runAsNonRoot"])
        self.assertEqual(c["command"][:2], ["python3", "/etc/platform-health-report/platform_health_report.py"])
        script = k[("ConfigMap", "platform-health-report", "shc-ops")]["data"]["platform_health_report.py"]
        with open(os.path.join(CHART, "files/platform_health_report.py")) as f:
            self.assertEqual(script.strip(), f.read().strip())

    def test_on_another_cluster(self):
        k = mine(render(*(HUB + ["--set", R + "servicesCluster.caKey=ca.crt", "--set", R + "intervalMinutes=10",
                                 "--set", R + "receiverCheck=true"])))
        self.assertFalse([x for x in k if x[2] == "platform"])
        self.assertFalse([x for x in k if x[1].startswith("platform-health-writer-")])
        cfg = json.loads(k[("ConfigMap", "platform-health-report", "shc-ops")]["data"]["config.json"])
        self.assertEqual((cfg["cluster"], cfg["targetMode"], cfg["targetServerKey"], cfg["targetTokenKey"],
                          cfg["targetCAKey"], cfg["intervalSeconds"], cfg["receiverCheck"], cfg["targetConfigMap"],
                          cfg["targetKey"]),
                         ("hub", "remote", "server", "token", "ca.crt", 600, True, "shc-platform-health-hub",
                          "report.json"))
        spec = k[("CronJob", "platform-health-report", "shc-ops")]["spec"]
        self.assertEqual(spec["schedule"], "*/10 * * * *")
        pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        vol = [v for v in pod["volumes"] if v["name"] == "services-cluster"][0]
        self.assertEqual(vol["secret"], {"secretName": "services-api", "optional": True})
        self.assertIn({"name": "services-cluster", "mountPath": "/etc/services-cluster", "readOnly": True},
                      pod["containers"][0]["volumeMounts"])

    def test_cluster_name_defaults_to_the_clusters(self):
        k = mine(render(*(ON + ["--set", R + "onServicesCluster=true", "--set", R + "readerGroups[0]=g",
                                "--set", "cluster.name=acme-services"])))
        cfg = json.loads(k[("ConfigMap", "platform-health-report", "shc-ops")]["data"]["config.json"])
        self.assertEqual(cfg["cluster"], "acme-services")

    def test_missing_or_wrong_settings_fail_with_their_names(self):
        def without(args, setting):
            out, i = [], 0
            while i < len(args):
                if args[i] == "--set" and args[i + 1].startswith(R + setting + "="):
                    i += 2
                    continue
                out.append(args[i])
                i += 1
            return out
        cases = [
            (without(SERVICES, "cluster"), R + "cluster is required"),
            (without(SERVICES, "namespace"), R + "namespace is required"),
            (without(SERVICES, "onServicesCluster"), R + "onServicesCluster must be true or false"),
            (without(SERVICES, "readerGroups[0]"), R + "readerGroups needs at least one group"),
            (SERVICES + ["--set", R + "servicesCluster.secret=x"], R + "servicesCluster is for another cluster"),
            (SERVICES + ["--set", R + "writers[0]=Hub_1"], R + "writers"),
            (SERVICES + ["--set", R + "writers[0]=services"], R + "writers: services is this cluster"),
            (SERVICES + ["--set", R + "writers[0]=hub", "--set", R + "writers[1]=hub"], R + "writers: a name is listed twice"),
            (SERVICES + ["--set", R + "intervalMinutes=7"], R + "intervalMinutes must be one of"),
            (SERVICES + ["--set", R + "intervalMinutes=0"], R + "intervalMinutes must be a whole number"),
            (SERVICES + ["--set", R + "cluster=Hub"], R + "cluster \"Hub\""),
            (SERVICES + ["--set", R + "receiverCheck=yes"], R + "receiverCheck must be true or false"),
            (SERVICES + ["--set", R + "image="], R + "image is required"),
            (without(HUB, "servicesCluster.secret"), R + "servicesCluster.secret is required"),
            (without(HUB, "servicesCluster.serverKey"), R + "servicesCluster.serverKey is required"),
            (without(HUB, "servicesCluster.tokenKey"), R + "servicesCluster.tokenKey is required"),
            (HUB + ["--set", R + "readerGroups[0]=g"], R + "writers and readerGroups belong to the services cluster"),
            (HUB + ["--set", R + "writers[0]=edge"], R + "writers and readerGroups belong to the services cluster"),
        ]
        for args, want in cases:
            with self.subTest(want=want):
                p = render(*args, ok=False)
                self.assertNotEqual(p.returncode, 0, want)
                self.assertIn(want, p.stderr)

    def test_no_lab_values(self):
        for args in (SERVICES + ["--set", R + "writers[0]=hub"], HUB):
            text = yaml.safe_dump_all([d for d in mine(render(*args)).values()])
            self.assertIsNone(LAB.search(text), LAB.search(text))

    def test_the_writer_has_its_own_alert(self):
        # not a check (its findings are not read), and not a backup: the backup alerts leave it out, and its own alert
        # says what a failed run means
        rules = {r["alert"]: r for d in render(*SERVICES) if d["kind"] == "PrometheusRule"
                 for g in d["spec"]["groups"] for r in g["rules"]}
        self.assertFalse(re.fullmatch("eda-activations|eda-api-check|identity-write-probe-.+", "platform-health-report"))
        for name in ("ShcBackupJobFailed", "ShcBackupJobNeverSucceeded", "ShcBackupStale"):
            self.assertIn('cronjob!~"eda-activations|eda-api-check|identity-write-probe-.+|platform-health-report"',
                          rules[name]["expr"], name)
        own = rules["ShcPlatformHealthReportFailed"]
        self.assertEqual(own["expr"].count('cronjob="platform-health-report"'), 3)
        self.assertIn("unless on (namespace, cronjob) kube_cronjob_status_last_successful_time", own["expr"])
        self.assertEqual(own["labels"]["severity"], "warning")


if __name__ == "__main__":
    unittest.main()
