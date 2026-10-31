"""The site job's read of the tenant DNS controller's health (cloudinfrastructure_provision/tenant_dns_health.yml):
status.realization.tenantDns from the ConfigMap tenant-dns-status, the TenantDns condition (True/Ready, False/
DNSReconcileFailed for an error newer than the last success, no success yet or a stale success, Unknown/NotReported
when the record went or cannot be read), the change check of the partial write and the wiring in main.yml. The plays
run in ansible-playbook (skipped without it)."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
CI = os.path.join(ROLES, "cloudinfrastructure_provision", "tasks")
FILE = os.path.join(CI, "tenant_dns_health.yml")
NOW = 1_800_000_000


def ts(offset):
    """RFC3339 (the controller's format) of NOW + offset seconds."""
    t = datetime.datetime.fromtimestamp(NOW + offset, datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def cm(**data):
    return {"resources": [{"metadata": {"name": "tenant-dns-status"}, "data": data}]}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Judge(unittest.TestCase):
    DEFAULTS = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))["fabric_realization_defaults"]

    def judge(self, result, status=None, realization=None):
        tasks = load(FILE)
        settings = by_name(tasks, "The tenant DNS health settings and the last record")
        judge = by_name(tasks, "The tenant DNS health record and condition")
        rec = dict(by_name(tasks, "Record the tenant DNS health of this site"))
        out = {"name": "the status write", "vars": rec["vars"],
               "ansible.builtin.set_fact": {"w_status": "{{ _status }}", "w_changed": "{{ _changed | bool }}"}}
        extra = {"fabric_realization_defaults": self.DEFAULTS, "ci_tdns_now": NOW, "_ci_tdns_cm": result,
                 "cloudinfrastructure_cr_lookup": {"resources": [{"status": status or {}}]}}
        if realization is not None:
            extra["fabric_realization"] = realization
        return run_play([settings, judge, out, write_out(
            "{'rec': ci_tdns, 'cond': ci_tdns_condition, 'ns': ci_tdns_namespace, 'status': w_status, "
            "'changed': w_changed}")], extra)

    def test_healthy(self):
        out = self.judge(cm(lastSuccess=ts(-60), lastError="X: old", lastErrorAt=ts(-3600), refused="2", ptrs="5"))
        rec = {"lastSuccess": ts(-60), "lastError": "X: old", "lastErrorAt": ts(-3600), "refused": 2, "ptrs": 5}
        self.assertEqual(out["rec"], rec)
        self.assertEqual(out["ns"], "shc-tenant-dns")
        self.assertEqual((out["cond"]["type"], out["cond"]["status"], out["cond"]["reason"]), ("TenantDns", "True", "Ready"))
        self.assertIn("5 PTR(s), 2 object(s) with a refusal", out["cond"]["message"])
        self.assertEqual(out["status"], {"realization": {"tenantDns": rec}, "conditions": [out["cond"]]})
        self.assertTrue(out["changed"])
        # the same record and condition again: nothing to write
        again = self.judge(cm(lastSuccess=ts(-60), lastError="X: old", lastErrorAt=ts(-3600), refused="2", ptrs="5"),
                           status={"realization": {"tenantDns": rec}, "conditions": [out["cond"]]})
        self.assertFalse(again["changed"])

    def test_an_error_newer_than_the_last_success(self):
        out = self.judge(cm(lastSuccess=ts(-600), lastError="HttpException: designate down", lastErrorAt=ts(-30),
                            refused="0", ptrs="1"))
        self.assertEqual((out["cond"]["status"], out["cond"]["reason"]), ("False", "DNSReconcileFailed"))
        self.assertIn(f"the last reconcile failed at {ts(-30)}", out["cond"]["message"])
        self.assertIn("designate down", out["cond"]["message"])

    def test_a_stale_success(self):
        out = self.judge(cm(lastSuccess=ts(-31 * 60), lastError="", lastErrorAt="", refused="0", ptrs="0"))
        self.assertEqual((out["cond"]["status"], out["cond"]["reason"]), ("False", "DNSReconcileFailed"))
        self.assertIn("more than 30 minutes", out["cond"]["message"])
        self.assertEqual(out["rec"]["lastError"], None)
        # staleMinutes and the namespace come from the realization
        out = self.judge(cm(lastSuccess=ts(-31 * 60), refused="0", ptrs="0"),
                         realization={"dns": {"tenantDnsNamespace": "dns-ctl", "reverse6": {"staleMinutes": 60}}})
        self.assertEqual((out["cond"]["status"], out["ns"]), ("True", "dns-ctl"))
        out = self.judge(cm(lastSuccess=ts(-11 * 60)), realization={"dns": {"reverse6": {"staleMinutes": 10}}})
        self.assertEqual(out["cond"]["status"], "False")

    def test_no_success_yet(self):
        out = self.judge(cm(lastSuccess="", lastError="KeyError: 'x'", lastErrorAt=ts(-5), refused="0", ptrs="0"))
        self.assertEqual((out["cond"]["status"], out["cond"]["reason"]), ("False", "DNSReconcileFailed"))
        self.assertIn("has not reconciled successfully yet", out["cond"]["message"])
        self.assertEqual(out["rec"]["lastSuccess"], None)

    def test_no_configmap(self):
        out = self.judge({"resources": []})
        self.assertEqual((out["rec"], out["cond"]), ({}, {}))
        self.assertEqual(out["status"], {"realization": {"tenantDns": None}})
        self.assertFalse(out["changed"])  # never reported, nothing reported now: no write
        prev = {"type": "TenantDns", "status": "True", "reason": "Ready", "message": "last success"}
        out = self.judge({"resources": []}, status={"realization": {"tenantDns": {"lastSuccess": ts(-60)}},
                                                    "conditions": [{"type": "Ready", "status": "True"}, prev]})
        self.assertEqual((out["cond"]["status"], out["cond"]["reason"]), ("Unknown", "NotReported"))
        self.assertEqual(out["status"]["realization"], {"tenantDns": None})
        self.assertTrue(out["changed"])

    def test_not_readable_keeps_the_last_record(self):
        prev = {"lastSuccess": ts(-60), "lastError": None, "lastErrorAt": None, "refused": 0, "ptrs": 1}
        out = self.judge({"failed": True, "msg": "Forbidden"}, status={"realization": {"tenantDns": prev}})
        self.assertEqual(out["rec"], prev)
        self.assertEqual((out["cond"]["status"], out["cond"]["reason"]), ("Unknown", "NotReported"))
        self.assertIn("not readable: Forbidden", out["cond"]["message"])

    def test_the_whole_file_never_fails_the_job(self):
        """The site API does not answer: recorded as NotReported, the job goes on."""
        tasks = load(FILE)
        rec = by_name(tasks, "Record the tenant DNS health of this site")
        rec["ansible.builtin.debug"] = {"msg": "{{ rec_status }}"}  # the status module stands in as a debug
        rec["vars"] = dict(rec.get("vars") or {}, rec_status=rec.pop("hybridsovereign.shc.cr_status")["status"])
        out = run_play(tasks + [write_out("{'rec': ci_tdns, 'cond': ci_tdns_condition}")],
                       {"role_path": os.path.join(ROLES, "cloudinfrastructure_provision"), "ci_tdns_now": NOW,
                        "ci_k8s": {"host": "https://127.0.0.1:9", "api_key": "x"},
                        "cloudinfrastructure_cr_name": "site-a", "cloudinfrastructure_cr_namespace": "platform",
                        "cloudinfrastructure_cr_lookup": {"resources": [{"status": {}}]}})
        self.assertEqual(out["rec"], {})
        self.assertEqual((out["cond"]["status"], out["cond"]["reason"]), ("Unknown", "NotReported"))


class Wiring(unittest.TestCase):
    def test_wiring(self):
        main = load(os.path.join(CI, "main.yml"))
        inc = by_name(main, "Read the tenant DNS controller's health on this site")
        self.assertEqual(inc["ansible.builtin.include_tasks"], "tenant_dns_health.yml")
        self.assertEqual(inc["when"], "ci_cr_type == 'openshift'")
        tasks = load(FILE)
        rec = by_name(tasks, "Record the tenant DNS health of this site")
        self.assertTrue(rec["hybridsovereign.shc.cr_status"]["partial"])
        self.assertFalse(rec["failed_when"])
        self.assertEqual(rec["when"], "_changed | bool")
        read = by_name(tasks, "Read the tenant DNS controller's health")
        self.assertEqual((read["kubernetes.core.k8s_info"]["kind"], read["kubernetes.core.k8s_info"]["name"]),
                         ("ConfigMap", "tenant-dns-status"))
        self.assertFalse(read["failed_when"])
        blk = by_name(tasks, "Read and judge the tenant DNS controller's health")
        self.assertEqual([t["name"] for t in blk["rescue"]], ["Record why the tenant DNS health was not judged"])

    def test_reasons_registered(self):
        path = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                            "plugins", "module_utils", "shc_status_reasons.py")
        ns = {"__name__": "reasons"}
        with open(path) as f:
            exec(compile(f.read(), path, "exec"), ns)  # noqa: S102 - repository file
        self.assertEqual(ns["PART_REASONS"]["DNSReconcileFailed"]["actor"], "P")
        self.assertIn("NotReported", ns["PART_REASONS"])
        self.assertIsNotNone(ns["REASONS"].get("Ready"))

    def test_the_probe_account_may_read_the_record(self):
        values = load(os.path.join(ROOT, "bootstrap", "helm", "central", "values.yaml"))
        rules = values["iaasClusterDefaults"]["cloudInfra"]["rules"]
        self.assertIn({"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get"],
                       "resourceNames": ["tenant-dns-status"]}, rules)


if __name__ == "__main__":
    unittest.main()
