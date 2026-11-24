"""shc-cluster-base: the platform's own health (templates/platform-health.yaml, ops.platformHealth). Every switch off
renders nothing of it; each switch renders only its own rules or CronJob; the checks get exactly the rights they
use; missing settings fail with their names; no lab value is in the rendered texts; and the rules pass
`promtool check rules` and `promtool test rules` on made-up series (promtool on PATH, or the Prometheus image
through podman; skipped when neither is there): each alert fires and clears, the excluded cases never fire, and the
generic job alerts leave the checks' CronJobs to their own alerts. The receiver switch no longer exists (OpenShift's
own AlertmanagerReceiversNotConfigured covers it)."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

from test_fabric_health_alerts import promtool

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CHART = os.path.join(ROOT, "charts/charts/shc-cluster-base")
BASE = ["--set", "layers.base=false", "--set", "layers.certs=false", "--set", "layers.ops=true"]
P = "ops.platformHealth."
EDA = ["--set", P + "edaActivations.enabled=true", "--set", P + "edaActivations.url=https://automation.example.com",
       "--set", P + "edaActivations.automationNamespace=automation",
       "--set", P + "edaActivations.credentialSecret=eda-check-account"]
KC = ["--set", P + "identityWriteProbe.enabled=true",
      "--set", P + "identityWriteProbe.keycloaks[0].name=identity",
      "--set", P + "identityWriteProbe.keycloaks[0].url=https://sso.example.com",
      "--set", P + "identityWriteProbe.keycloaks[0].realm=platform",
      "--set", P + "identityWriteProbe.keycloaks[0].clientId=write-check",
      "--set", P + "identityWriteProbe.keycloaks[0].credentialSecret=kc-write-check"]
ALL_RULES = ["--set", P + "volumes.enabled=true", "--set", P + "databaseBackups.enabled=true",
             "--set", P + "aggregatedAPIs.enabled=true"] + EDA + KC
LAB = re.compile(r"signal9|172\.18\.|2602:0{0,3}81f:|central-rhbk|services-rhbk|services-quay|shc-central")


def render(*args, ok=True):
    p = subprocess.run(["helm", "template", "t", CHART] + BASE + list(args), capture_output=True, text=True,
                       timeout=120)
    if ok and p.returncode != 0:
        raise AssertionError(p.stderr)
    if not ok:
        return p
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def by_key(docs):
    return {(d["kind"], d["metadata"]["name"], d["metadata"].get("namespace")): d for d in docs}


def health_rules(docs):
    pr = [d for d in docs if d["kind"] == "PrometheusRule" and d["metadata"]["name"] == "shc-platform-health"]
    return {r["alert"]: r for g in (pr[0]["spec"]["groups"] if pr else []) for r in g["rules"]}


NEW = {"shc-platform-health", "eda-activations", "eda-api-check", "eda-activations-state", "shc-ops-eda-activations",
       "identity-write-probe", "identity-write-probe-identity"}


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class Render(unittest.TestCase):
    def test_off_renders_nothing_of_it(self):
        names = {d["metadata"]["name"] for d in render()}
        self.assertFalse(names & NEW)

    def test_each_switch_alone(self):
        groups = {"volumes": {"ShcVolumeFilling", "ShcVolumeAlmostFull", "ShcVolumeFillsSoon"},
                  "databaseBackups": {"ShcDatabaseBackupFailed", "ShcDatabaseBackupOverdue",
                                      "ShcDatabaseBackupNotScheduled"},
                  "aggregatedAPIs": {"ShcAggregatedAPIUnavailable"}}
        for key, alerts in groups.items():
            with self.subTest(key):
                docs = render("--set", P + key + ".enabled=true")
                self.assertEqual(set(health_rules(docs)), alerts)
                self.assertFalse({d["metadata"]["name"] for d in docs if d["kind"] == "CronJob"} &
                                 {"eda-activations", "eda-api-check", "identity-write-probe-identity"})
        docs = render(*EDA)
        self.assertEqual(set(health_rules(docs)), {"ShcEventActivationsNotRunning", "ShcAutomationAPIUnavailable"})
        docs = render(*KC)
        self.assertEqual(set(health_rules(docs)), {"ShcIdentityWriteCheckFailed"})
        # the former receiver switch renders nothing
        self.assertEqual(health_rules(render("--set", P + "receiver.enabled=true")), {})

    def test_values_reach_the_rules(self):
        rules = health_rules(render("--set", P + "volumes.enabled=true", "--set", P + "volumes.warningPercent=70",
                                    "--set", P + "volumes.namespaces=platform-.*",
                                    "--set", P + "volumes.fillsSoonMinPercent=60",
                                    "--set", P + "aggregatedAPIs.enabled=true", "--set", P + "aggregatedAPIs.flapWindow=20m",
                                    "--set", P + "aggregatedAPIs.unavailableFraction=0.3"))
        self.assertIn("> 70 <= 90", rules["ShcVolumeFilling"]["expr"])
        self.assertIn('namespace=~"platform-.*"', rules["ShcVolumeFilling"]["expr"])
        self.assertIn("* 100) > 60)", rules["ShcVolumeFillsSoon"]["expr"])
        self.assertIn("[20m])) >= 0.3", rules["ShcAggregatedAPIUnavailable"]["expr"])
        # a trend alone is a warning; critical only when the volume is over the critical threshold
        self.assertEqual(rules["ShcVolumeFillsSoon"]["labels"]["severity"], "warning")
        self.assertEqual(rules["ShcVolumeAlmostFull"]["labels"]["severity"], "critical")

    def test_activation_check(self):
        k = by_key(render(*EDA))
        cfg = yaml.safe_load(k[("ConfigMap", "eda-activations", "shc-ops")]["data"]["config.json"])
        self.assertEqual((cfg["url"], cfg["automationNamespace"], cfg["stateNamespace"], cfg["maxRepairsPerHour"],
                          cfg["maxRepairsPerDay"], cfg["allowNoActivations"], cfg["removeStaleJobs"], cfg["caFile"]),
                         ("https://automation.example.com", "automation", "shc-ops", 3, 6, False, False, ""))
        job = k[("CronJob", "eda-activations", "shc-ops")]["spec"]["jobTemplate"]["spec"]
        # the cap's window is an hour plus the Job's deadline: the script gets the deadline the Job has
        self.assertEqual(cfg["runDeadlineSeconds"], job["activeDeadlineSeconds"])
        check = job["template"]["spec"]
        self.assertEqual(check["serviceAccountName"], "eda-activations")
        c = check["containers"][0]
        self.assertEqual(c["command"][-2:], ["--mode", "check"])
        self.assertEqual(c["image"], "registry.redhat.io/ubi9/python-312-minimal:latest")
        self.assertTrue(c["securityContext"]["runAsNonRoot"])
        self.assertEqual([v["secret"]["secretName"] for v in check["volumes"] if "secret" in v], ["eda-check-account"])
        api = k[("CronJob", "eda-api-check", "shc-ops")]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertFalse(api["automountServiceAccountToken"])
        self.assertEqual(api["containers"][0]["command"][-2:], ["--mode", "api"])
        # exactly the rights it uses: its own state ConfigMap, and listing Jobs in the platform's namespace
        state = k[("Role", "eda-activations-state", "shc-ops")]["rules"]
        self.assertEqual(state, [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["create"]},
                                 {"apiGroups": [""], "resources": ["configmaps"],
                                  "resourceNames": ["eda-activations-state"], "verbs": ["get", "update"]}])
        jobs = k[("Role", "shc-ops-eda-activations", "automation")]["rules"]
        self.assertEqual(jobs, [{"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["list"]}])
        b = k[("RoleBinding", "shc-ops-eda-activations", "automation")]
        self.assertEqual(b["subjects"], [{"kind": "ServiceAccount", "name": "eda-activations", "namespace": "shc-ops"}])

    def test_stale_job_removal_is_off_by_default_and_grants_delete_only_when_on(self):
        k = by_key(render(*EDA, "--set", P + "edaActivations.removeStaleJobs=true",
                          "--set", P + "edaActivations.caConfigMap=gateway-ca"))
        self.assertEqual(k[("Role", "shc-ops-eda-activations", "automation")]["rules"][0]["verbs"], ["list", "delete"])
        cfg = yaml.safe_load(k[("ConfigMap", "eda-activations", "shc-ops")]["data"]["config.json"])
        self.assertEqual((cfg["removeStaleJobs"], cfg["caFile"]), (True, "/etc/eda-ca/ca.crt"))

    def test_identity_probe(self):
        k = by_key(render(*KC))
        cm = k[("ConfigMap", "identity-write-probe", "shc-ops")]["data"]
        cfg = yaml.safe_load(cm["identity.json"])
        self.assertEqual((cfg["url"], cfg["realm"], cfg["clientId"], cfg["attribute"]),
                         ("https://sso.example.com", "platform", "write-check", "lastName"))
        spec = k[("CronJob", "identity-write-probe-identity", "shc-ops")]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertNotIn("serviceAccountName", spec)
        self.assertEqual([v["secret"]["secretName"] for v in spec["volumes"] if "secret" in v], ["kc-write-check"])
        self.assertFalse([d for d in k if d[0] in ("Role", "ClusterRole") and "identity" in d[1]])

    def test_required_settings_fail_with_their_names(self):
        cases = [
            (EDA[:-2], "ops.platformHealth.edaActivations.credentialSecret is required"),
            (["--set", P + "edaActivations.enabled=true"], "ops.platformHealth.edaActivations.automationNamespace"),
            (["--set", P + "identityWriteProbe.enabled=true"], "needs at least one Keycloak"),
            (KC[:-2], "keycloaks[identity].credentialSecret is required"),
            (KC + ["--set", P + "identityWriteProbe.keycloaks[0].name=Bad_Name"], "lower-case letters"),
        ]
        for args, msg in cases:
            with self.subTest(msg):
                p = render(*args, ok=False)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(msg, p.stderr)

    def test_settings_of_the_activation_check_are_checked_with_their_names(self):
        E = P + "edaActivations."
        cases = [
            (["--set-string", E + "maxRepairsPerHour=3"], "edaActivations.maxRepairsPerHour must be a whole number"),
            (["--set", E + "maxRepairsPerHour=null"], "edaActivations.maxRepairsPerHour must be a whole number"),
            (["--set", E + "maxRepairsPerDay=2.5"], "edaActivations.maxRepairsPerDay must be a whole number"),
            (["--set", E + "startTimeoutSeconds=0"], "edaActivations.startTimeoutSeconds must be a whole number of at least 1"),
            (["--set", E + "notRunningMinutes=-1"], "edaActivations.notRunningMinutes must be a whole number of at least 0"),
            (["--set", E + "transitionalMinutes=true"], "edaActivations.transitionalMinutes must be a whole number"),
            (["--set", E + "url=http://automation.example.com"], "edaActivations.url must be an https URL"),
            (["--set", E + "url=automation.example.com"], "edaActivations.url must be an https URL"),
            (["--set-string", E + "removeStaleJobs=false"], "edaActivations.removeStaleJobs must be true or false"),
            (["--set", E + "allowNoActivations=null"], "edaActivations.allowNoActivations must be true or false"),
            (["--set-string", E + "cannotRunPatterns=pull failed"], "edaActivations.cannotRunPatterns must be a list"),
        ]
        for args, msg in cases:
            with self.subTest(args=args):
                p = render(*EDA, *args, ok=False)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(msg, p.stderr)
        # whole numbers, also from a values file (where the template sees them as floats), render as their digits
        tmp = tempfile.mkdtemp(prefix="shc-values-")
        self.addCleanup(shutil.rmtree, tmp, True)
        values = os.path.join(tmp, "values.yaml")
        with open(values, "w") as f:
            yaml.safe_dump({"ops": {"platformHealth": {"edaActivations": {"maxRepairsPerDay": 10000000,
                                                                          "transitionalMinutes": 2.5}}}}, f)
        p = render(*EDA, "-f", values, ok=False)
        self.assertIn("edaActivations.transitionalMinutes must be a whole number", p.stderr)
        with open(values, "w") as f:
            yaml.safe_dump({"ops": {"platformHealth": {"edaActivations": {"maxRepairsPerDay": 10000000}}}}, f)
        k = by_key(render(*EDA, "-f", values, "--set", E + "maxRepairsPerHour=4"))
        raw = k[("ConfigMap", "eda-activations", "shc-ops")]["data"]["config.json"]
        self.assertIn('"maxRepairsPerDay": 10000000', raw)
        cfg = yaml.safe_load(raw)
        self.assertEqual((cfg["maxRepairsPerHour"], cfg["notRunningMinutes"]), (4, 4))
        self.assertTrue(all(isinstance(cfg[x], int) for x in ("maxRepairsPerHour", "maxRepairsPerDay",
                                                              "notRunningMinutes", "transitionalMinutes",
                                                              "startTimeoutSeconds")))

    def test_no_lab_value_in_the_texts(self):
        docs = render(*ALL_RULES)
        for d in docs:
            if d["metadata"]["name"] in NEW:
                text = yaml.safe_dump(d)
                self.assertIsNone(LAB.search(text), d["metadata"]["name"])

    def test_generic_job_alerts_leave_the_checks_alone(self):
        rules = {r["alert"]: r for d in render() if d["metadata"]["name"] == "shc-platform"
                 for g in d["spec"]["groups"] for r in g["rules"]}
        for name in ("ShcBackupJobFailed", "ShcBackupJobNeverSucceeded", "ShcBackupStale"):
            # the checks and the banner's report (which has its own alert) are left out
            self.assertIn('cronjob!~"eda-activations|eda-api-check|identity-write-probe-.+|platform-health-report"',
                          rules[name]["expr"])


def cron(ns, name, sched, success=None):
    out = [{"series": 'kube_cronjob_status_last_schedule_time{namespace="%s",cronjob="%s"}' % (ns, name),
            "values": sched}]
    if success is not None:
        out.append({"series": 'kube_cronjob_status_last_successful_time{namespace="%s",cronjob="%s"}' % (ns, name),
                    "values": success})
    return out


def alternating(minutes):
    """(schedule, success) value strings of a 5-minute CronJob whose every other run fails: run k is scheduled at
    minute 5k; an even one succeeds a minute later, an odd one never does."""
    sched, success, last = [], [], 0
    for m in range(minutes + 1):
        k = m // 5
        sched.append(str(300 * k))
        if k % 2 == 0 and m >= 5 * k + 1:
            last = 300 * k
        success.append(str(last))
    return " ".join(sched), " ".join(success)


def kubelet(ns, pvc, used, cap="100x120", avail=None):
    out = [{"series": 'kubelet_volume_stats_used_bytes{namespace="%s",persistentvolumeclaim="%s",node="n1"}' % (ns, pvc),
            "values": used},
           {"series": 'kubelet_volume_stats_capacity_bytes{namespace="%s",persistentvolumeclaim="%s",node="n1"}' % (ns, pvc),
            "values": cap}]
    if avail:
        out.append({"series": 'kubelet_volume_stats_available_bytes{namespace="%s",persistentvolumeclaim="%s",node="n1"}'
                              % (ns, pvc), "values": avail})
    return out


def tests(rules):
    def a(name, eval_time, alerts):
        out = []
        for labels, summary in alerts:
            desc = re.sub(r"\{\{ \$labels\.(\w+) \}\}", lambda m: labels[m.group(1)],
                          rules[name]["annotations"]["description"])
            out.append({"exp_labels": dict(labels, severity=rules[name]["labels"]["severity"]),
                        "exp_annotations": {"summary": summary, "description": desc}})
        return {"eval_time": eval_time, "alertname": name, "exp_alerts": out}

    ops = "shc-ops"
    ACT = ("shc-ops/eda-activations: an event activation is not running after repair, cannot run, or has two pods, "
           "or the check itself did not complete")
    API = "shc-ops/eda-api-check: the automation platform's event-automation API does not answer"
    HEALTH = "This cluster's health report for the admin pages is not being written"
    KCS = "shc-ops/identity-write-probe-%s: the identity write check failed"
    return {"rule_files": ["rules.yml"], "evaluation_interval": "1m", "tests": [
        # volumes: 85 % for a while then 50 % (warning fires, clears); 95 % (critical); a read-only-many volume and
        # an OpenShift namespace at 95 % never fire
        {"interval": "1m",
         "input_series": kubelet("platform-db", "data", "85x40 50x40") + kubelet("platform-db", "repo", "95x80")
                         + kubelet("platform-ro", "shared", "95x80") + kubelet("openshift-x", "v", "95x80")
                         + [{"series": 'kube_persistentvolumeclaim_access_mode{namespace="platform-ro",'
                                       'persistentvolumeclaim="shared",access_mode="ReadOnlyMany"}', "values": "1x80"}],
         "alert_rule_test": [
             a("ShcVolumeFilling", "10m", []),
             a("ShcVolumeFilling", "20m", [({"namespace": "platform-db", "persistentvolumeclaim": "data"},
                                            "Volume platform-db/data is 85 % used")]),
             a("ShcVolumeFilling", "60m", []),
             a("ShcVolumeAlmostFull", "7m", [({"namespace": "platform-db", "persistentvolumeclaim": "repo"},
                                              "Volume platform-db/repo is 95 % used")]),
         ]},
        # a volume two thirds full losing 1 unit of 3000 a minute fills in under a day: critical after its hour; a flat
        # one never; a backup repository at 40 % that takes a 5-unit backup in 30 minutes never (the usage guard); the
        # same filling volume mounted read-only by many never
        {"interval": "1m",
         "input_series": kubelet("platform-db", "wal", "2000x200", "3000x200", "1000-1x200")
                         + kubelet("platform-db", "flat", "2000x200", "3000x200", "1000x200")
                         + kubelet("platform-db", "repo", "40+0.1666x30 45x170", "100x200", "60-0.1666x30 55x170")
                         + kubelet("platform-ro", "shared", "2000x200", "3000x200", "1000-1x200")
                         + [{"series": 'kube_persistentvolumeclaim_access_mode{namespace="platform-ro",'
                                       'persistentvolumeclaim="shared",access_mode="ReadOnlyMany"}', "values": "1x200"}],
         "alert_rule_test": [
             a("ShcVolumeFillsSoon", "30m", []),
             a("ShcVolumeFillsSoon", "90m", [({"namespace": "platform-db", "persistentvolumeclaim": "wal"},
                                              "Volume platform-db/wal fills within 3 days at its present rate")]),
         ]},
        # backups: a failed full backup (then a success clears it), a suspended one never alerts, a database
        # with a repository host and no full backup CronJob
        {"interval": "1m",
         "input_series": cron("platform-db", "app-pg-repo1-full", "1000x300", "500x250 1000x50")
                         + cron("platform-db", "old-pg-repo1-full", "1000x300", "500x300")
                         + [{"series": 'kube_cronjob_spec_suspend{namespace="platform-db",cronjob="old-pg-repo1-full"}',
                             "values": "1x300"},
                            {"series": 'kube_statefulset_replicas{namespace="platform-db",statefulset="app-pg-repo-host"}',
                             "values": "1x400"},
                            {"series": 'kube_cronjob_info{namespace="platform-db",cronjob="app-pg-repo1-full"}',
                             "values": "1x400"},
                            {"series": 'kube_statefulset_replicas{namespace="platform-db",statefulset="bare-pg-repo-host"}',
                             "values": "1x400"}],
         "alert_rule_test": [
             a("ShcDatabaseBackupFailed", "170m", []),
             a("ShcDatabaseBackupFailed", "190m", [({"namespace": "platform-db", "cronjob": "app-pg-repo1-full"},
                                                    "Database backup platform-db/app-pg-repo1-full: the last run did not "
                                                    "succeed")]),
             a("ShcDatabaseBackupFailed", "260m", []),
             a("ShcDatabaseBackupNotScheduled", "5h", []),
             a("ShcDatabaseBackupNotScheduled", "6h10m", [({"namespace": "platform-db", "statefulset": "bare-pg-repo-host",
                                                            "postgrescluster": "bare-pg"},
                                                           "Database platform-db/bare-pg has no scheduled full backup")]),
         ]},
        # overdue: the last successful differential backup at time 0, nothing since (50 h allowed)
        {"interval": "1m",
         "input_series": cron("platform-db", "app-pg-repo1-diff", "0x3200", "0x3200")
                         + cron("platform-db", "app-pg-repo1-full", "0x3200", "0x3200")
                         # a suspended one (its database shut down) is never overdue
                         + cron("platform-db", "old-pg-repo1-diff", "0x3200", "0x3200")
                         + [{"series": 'kube_cronjob_spec_suspend{namespace="platform-db",cronjob="old-pg-repo1-diff"}',
                             "values": "1x3200"}],
         "alert_rule_test": [
             a("ShcDatabaseBackupOverdue", "50h", []),
             a("ShcDatabaseBackupOverdue", "52h", [({"namespace": "platform-db", "cronjob": "app-pg-repo1-diff"},
                                                    "Database backup platform-db/app-pg-repo1-diff: no successful run "
                                                    "for 2d 4h 0m 0s")]),
         ]},
        # aggregated APIs, each with an hour of history unless new: unavailable from minute 80 on (fires at minute 90,
        # while still down); a new APIService that is not ready for its first 6 minutes (quiet: the share of the
        # window counts only for an APIService that existed for the whole window); one unavailable every other minute
        # from minute 60 (fires once half of a whole window was unavailable); a 45-minute outage (held after the
        # recovery while at least half of the last hour was unavailable: about 30 minutes)
        {"interval": "1m",
         "input_series": [{"series": 'aggregator_unavailable_apiservice{name="v1.down.io",instance="a"}',
                           "values": "0x80 1x80"},
                          {"series": 'aggregator_unavailable_apiservice{name="v1.down.io",instance="b"}',
                           "values": "0x160"},
                          {"series": 'aggregator_unavailable_apiservice{name="v1.new.io",instance="a"}',
                           "values": "_x90 1x5 0x64"},
                          {"series": 'aggregator_unavailable_apiservice{name="v1.half.io",instance="a"}',
                           "values": "0x60 " + " ".join(["1 0"] * 50)},
                          {"series": 'aggregator_unavailable_apiservice{name="v1.outage.io",instance="a"}',
                           "values": "0x60 1x44 0x55"}],
         "alert_rule_test": [
             a("ShcAggregatedAPIUnavailable", "5m", []),
             a("ShcAggregatedAPIUnavailable", "88m", [({"name": "v1.outage.io"}, "Aggregated API v1.outage.io is unavailable")]),
             a("ShcAggregatedAPIUnavailable", "92m", [({"name": "v1.down.io"}, "Aggregated API v1.down.io is unavailable"),
                                                      ({"name": "v1.outage.io"}, "Aggregated API v1.outage.io is unavailable")]),
             a("ShcAggregatedAPIUnavailable", "101m", [({"name": "v1.down.io"}, "Aggregated API v1.down.io is unavailable"),
                                                       ({"name": "v1.outage.io"}, "Aggregated API v1.outage.io is unavailable")]),
             a("ShcAggregatedAPIUnavailable", "130m", [({"name": "v1.down.io"}, "Aggregated API v1.down.io is unavailable"),
                                                       ({"name": "v1.half.io"}, "Aggregated API v1.half.io is unavailable"),
                                                       ({"name": "v1.outage.io"}, "Aggregated API v1.outage.io is unavailable")]),
             a("ShcAggregatedAPIUnavailable", "140m", [({"name": "v1.down.io"}, "Aggregated API v1.down.io is unavailable"),
                                                       ({"name": "v1.half.io"}, "Aggregated API v1.half.io is unavailable")]),
         ]},
        # the checks: the activation check failing alone fires after 10 minutes; once the API check fails too, only the
        # API alert holds (one fault, one alert); the identity probe that never succeeded fires; the generic job alert
        # stays quiet for the checks and still fires for an ordinary ops job
        {"interval": "1m",
         "input_series": cron(ops, "eda-activations", "0+60x60", "0x60")
                         + cron(ops, "eda-api-check", "0+60x60", "0+60x20 1200x40")
                         + cron(ops, "identity-write-probe-identity", "0+60x60")
                         + cron(ops, "etcd-backup", "1000x100", "500x100"),
         "alert_rule_test": [
             a("ShcEventActivationsNotRunning", "9m", []),
             a("ShcEventActivationsNotRunning", "15m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
             a("ShcEventActivationsNotRunning", "35m", []),
             a("ShcAutomationAPIUnavailable", "15m", []),
             a("ShcAutomationAPIUnavailable", "35m", [({"namespace": ops, "cronjob": "eda-api-check"}, API)]),
             a("ShcIdentityWriteCheckFailed", "12m", [({"namespace": ops, "cronjob": "identity-write-probe-identity"},
                                                      KCS % "identity")]),
             {"eval_time": "90m", "alertname": "ShcBackupJobFailed", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": ops, "cronjob": "etcd-backup"},
                  "exp_annotations": {"summary": "shc-ops/etcd-backup: the last run failed",
                                      "description": "The most recent run of CronJob etcd-backup did not succeed. "
                                                     "The failed pod's termination message or its log says why."}}]},
             {"eval_time": "90m", "alertname": "ShcBackupJobNeverSucceeded", "exp_alerts": []},
         ]},
        # the banner's report: a failed last run, and one that never succeeded, fire its own alert after 15 minutes;
        # the backup alerts stay quiet for it
        {"interval": "1m",
         "input_series": cron(ops, "platform-health-report", "1000x100", "500x100"),
         "alert_rule_test": [
             a("ShcPlatformHealthReportFailed", "10m", []),
             a("ShcPlatformHealthReportFailed", "20m", [({"namespace": ops, "cronjob": "platform-health-report"},
                                                        HEALTH)]),
             {"eval_time": "90m", "alertname": "ShcBackupJobFailed", "exp_alerts": []},
             {"eval_time": "90m", "alertname": "ShcBackupStale", "exp_alerts": []},
         ]},
        {"interval": "1m",
         "input_series": cron(ops, "platform-health-report", "1000x100"),
         "alert_rule_test": [
             a("ShcPlatformHealthReportFailed", "20m", [({"namespace": ops, "cronjob": "platform-health-report"},
                                                        HEALTH)]),
             {"eval_time": "90m", "alertname": "ShcBackupJobNeverSucceeded", "exp_alerts": []},
         ]},
        # the activation check failing while the API check is healthy but caught in flight once a cycle (scheduled, not
        # yet succeeded): the activation alert still fires; with the API check suspended it fires too
        {"interval": "1m",
         "input_series": cron(ops, "eda-activations", "0+60x60", "0x60")
                         + cron(ops, "eda-api-check", "0 0 0 0 0 300 300 300 300 300 600 600 600 600 600 900 900 900 900 900 1200 1200 1200 1200 1200 1500 1500 1500 1500 1500 1800 1800 1800 1800 1800 2100 2100 2100 2100 2100 2400 2400 2400 2400 2400 2700 2700 2700 2700 2700 3000 3000 3000 3000 3000 3300 3300 3300 3300 3300 3600", "0 0 0 0 0 0 300 300 300 300 300 600 600 600 600 600 900 900 900 900 900 1200 1200 1200 1200 1200 1500 1500 1500 1500 1500 1800 1800 1800 1800 1800 2100 2100 2100 2100 2100 2400 2400 2400 2400 2400 2700 2700 2700 2700 2700 3000 3000 3000 3000 3000 3300 3300 3300 3300 3300"),
         "alert_rule_test": [
             a("ShcEventActivationsNotRunning", "15m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
             a("ShcEventActivationsNotRunning", "40m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
             a("ShcAutomationAPIUnavailable", "40m", []),
         ]},
        # the API check suspended from the start (no run since): the activation alert is not hidden, also past the
        # stale window, and the API alert speaks for the suspended check
        {"interval": "1m",
         "input_series": cron(ops, "eda-activations", "0+60x100", "0x100")
                         + cron(ops, "eda-api-check", "0x100", "0x100")
                         + [{"series": 'kube_cronjob_spec_suspend{namespace="%s",cronjob="eda-api-check"}' % ops,
                             "values": "1x100"}],
         "alert_rule_test": [
             a("ShcEventActivationsNotRunning", "15m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
             a("ShcEventActivationsNotRunning", "90m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
             a("ShcAutomationAPIUnavailable", "15m", [({"namespace": ops, "cronjob": "eda-api-check"}, API)]),
             a("ShcAutomationAPIUnavailable", "90m", [({"namespace": ops, "cronjob": "eda-api-check"}, API)]),
         ]},
        # the API check fails from minute 11 and is suspended at minute 20, the activation check failing throughout:
        # the API check silences while it fails and is still scheduled, then no longer; the activation alert fires
        # ten minutes after the silence ends and stays
        {"interval": "1m",
         "input_series": cron(ops, "eda-activations", "0+60x200", "0x200")
                         + cron(ops, "eda-api-check", "0+60x20 1200x180", "0+60x10 600x190")
                         + [{"series": 'kube_cronjob_spec_suspend{namespace="%s",cronjob="eda-api-check"}' % ops,
                             "values": "0x19 1x181"}],
         "alert_rule_test": [
             a("ShcEventActivationsNotRunning", "25m", []),
             a("ShcEventActivationsNotRunning", "45m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
             a("ShcEventActivationsNotRunning", "199m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
             a("ShcAutomationAPIUnavailable", "199m", [({"namespace": ops, "cronjob": "eda-api-check"}, API)]),
         ]},
        # the API check failing every other run (every 5 minutes) while the activation check fails every run: the API
        # alert never holds its 10 minutes, so the activation alert must speak
        {"interval": "1m",
         "input_series": cron(ops, "eda-activations", "0+60x200", "0x200")
                         + cron(ops, "eda-api-check", *alternating(200)),
         "alert_rule_test": [
             a("ShcEventActivationsNotRunning", "15m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
             a("ShcEventActivationsNotRunning", "33m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
             a("ShcEventActivationsNotRunning", "199m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
             a("ShcAutomationAPIUnavailable", "199m", []),
         ]},
        # a check CronJob never scheduled: quiet until its first run is overdue by the stale window, then fires; one
        # whose first run is not due yet stays quiet; a suspended one that never ran speaks through the suspended
        # clause; the activation check never scheduled fires too
        {"interval": "1m",
         "input_series": [{"series": 'kube_cronjob_next_schedule_time{namespace="%s",cronjob="identity-write-probe-never"}'
                                     % ops, "values": "300x100"},
                          {"series": 'kube_cronjob_next_schedule_time{namespace="%s",cronjob="identity-write-probe-later"}'
                                     % ops, "values": "6000x100"},
                          {"series": 'kube_cronjob_next_schedule_time{namespace="%s",cronjob="identity-write-probe-off"}'
                                     % ops, "values": "300x100"},
                          {"series": 'kube_cronjob_spec_suspend{namespace="%s",cronjob="identity-write-probe-off"}' % ops,
                           "values": "1x100"},
                          {"series": 'kube_cronjob_next_schedule_time{namespace="%s",cronjob="eda-activations"}' % ops,
                           "values": "300x100"}]
                         + [{"series": 'kube_cronjob_%s{namespace="%s",cronjob="%s"}' % (m, ops, c), "values": v}
                            for c in ("identity-write-probe-never", "identity-write-probe-later",
                                      "identity-write-probe-off", "eda-activations")
                            for m, v in (("info", "1x100"), ("status_active", "0x100"))],
         "alert_rule_test": [
             a("ShcIdentityWriteCheckFailed", "4m", []),
             a("ShcIdentityWriteCheckFailed", "20m", [({"namespace": ops, "cronjob": "identity-write-probe-off"},
                                                      KCS % "off")]),
             a("ShcIdentityWriteCheckFailed", "60m", [({"namespace": ops, "cronjob": "identity-write-probe-off"},
                                                      KCS % "off")]),
             a("ShcIdentityWriteCheckFailed", "80m", [({"namespace": ops, "cronjob": "identity-write-probe-never"},
                                                      KCS % "never"),
                                                     ({"namespace": ops, "cronjob": "identity-write-probe-off"},
                                                      KCS % "off")]),
             a("ShcEventActivationsNotRunning", "60m", []),
             a("ShcEventActivationsNotRunning", "80m", [({"namespace": ops, "cronjob": "eda-activations"}, ACT)]),
         ]},
        # staleness: a check that is suspended (its last run fine) fires; one whose last success is more than an hour
        # old fires, even with no failed run; a fresh install (the CronJob exists, nothing ran yet) stays quiet
        {"interval": "1m",
         "input_series": cron(ops, "identity-write-probe-paused", "0+60x30", "0+60x30")
                         + [{"series": 'kube_cronjob_spec_suspend{namespace="%s",cronjob="identity-write-probe-paused"}' % ops,
                             "values": "0x5 1x25"},
                            {"series": 'kube_cronjob_spec_suspend{namespace="%s",cronjob="identity-write-probe-new"}' % ops,
                             "values": "0x100"},
                            {"series": 'kube_cronjob_info{namespace="%s",cronjob="identity-write-probe-new"}' % ops,
                             "values": "1x100"}]
                         + cron(ops, "identity-write-probe-stuck", "0x100", "0x100"),
         "alert_rule_test": [
             a("ShcIdentityWriteCheckFailed", "4m", []),
             a("ShcIdentityWriteCheckFailed", "20m", [({"namespace": ops, "cronjob": "identity-write-probe-paused"},
                                                      KCS % "paused")]),
             a("ShcIdentityWriteCheckFailed", "75m", [({"namespace": ops, "cronjob": "identity-write-probe-stuck"},
                                                      KCS % "stuck")]),
         ]},
    ]}


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class Rules(unittest.TestCase):
    def test_promtool(self):
        tool = promtool()
        if tool is None:
            self.skipTest("neither promtool nor the Prometheus image")
        docs = render(*ALL_RULES)
        groups = []
        for d in docs:
            if d["kind"] == "PrometheusRule" and d["metadata"]["name"] in ("shc-platform-health", "shc-platform"):
                groups += d["spec"]["groups"]
        rules = {r["alert"]: r for g in groups for r in g["rules"]}
        tmp = tempfile.mkdtemp(prefix="shc-promtool-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "rules.yml"), "w") as f:
            yaml.safe_dump({"groups": groups}, f)
        with open(os.path.join(tmp, "test.yml"), "w") as f:
            yaml.safe_dump(tests(rules), f)
        os.chmod(tmp, 0o755)
        for args in (("check", "rules", "rules.yml"), ("test", "rules", "test.yml")):
            p = subprocess.run(tool(tmp, *args), cwd=tmp, capture_output=True, text=True, timeout=300)
            self.assertEqual(p.returncode, 0, "promtool %s: %s %s" % (" ".join(args), p.stdout, p.stderr))


if __name__ == "__main__":
    unittest.main()
