"""shc-cluster-base: the fabric health metrics and alerts (templates/fabric-health.yaml, ops.fabricHealth). The chart
renders without them (switch off) and with them; the rules pass `promtool check rules` and `promtool test rules`
(promtool on PATH, or the Prometheus image through podman; skipped when neither is there): a node failure alerts,
a paused site's gateway (NodesHealthy Unknown) never does, the stale alert fires after three intervals, the pool and
IPv6 egress alerts."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CHART = os.path.join(ROOT, "charts/charts/shc-cluster-base")
IMAGE = os.environ.get("SHC_PROMTOOL_IMAGE", "quay.io/prometheus/prometheus:v3.5.0")
BASE = ["--set", "layers.base=false", "--set", "layers.certs=false", "--set", "layers.ops=true"]


def helm(*args):
    p = subprocess.run(["helm", "template", "t", CHART] + BASE + list(args), capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise AssertionError(p.stderr)
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def promtool():
    if shutil.which("promtool"):
        return lambda d, *a: ["promtool"] + list(a)
    if shutil.which("podman"):
        have = subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True).returncode == 0
        if have:
            return lambda d, *a: ["podman", "run", "--rm", "--network=none", "-v", "%s:/w:Z" % d, "-w", "/w",
                                  "--entrypoint", "promtool", IMAGE] + list(a)
    return None


def cond(kind, name, type_, status, reason, ns="sovereign-cloud"):
    return ('shc_fabric_condition{customresource_kind="%s",namespace="%s",name="%s",type="%s",status="%s",reason="%s"}'
            % (kind, ns, name, type_, status, reason))


TESTS = {
    "rule_files": ["rules.yml"],
    "evaluation_interval": "1m",
    "tests": [
        {"interval": "1m",
         "input_series": [
             {"series": cond("HybridFabric", "lab", "NodesHealthy", "False", "StartFailed"), "values": "1000x20"},
             {"series": cond("CloudGateway", "iaas1", "NodesHealthy", "Unknown", "SitePaused"), "values": "1000x20"},
             {"series": cond("CloudGateway", "iaas1", "NodesHealthy", "False", "NodeUnreachable"), "values": "1000x20"},
             {"series": cond("CloudGateway", "edge1", "NodesHealthy", "False", "SiteSessionsDown"), "values": "1000x20"},
             {"series": cond("HybridFabric", "lab", "IPv6EgressUpstream", "False", "Degraded"), "values": "1000x20"},
             {"series": 'shc_fabric_egress6_node_healthy{namespace="sovereign-cloud",name="lab",node="bgw-1"}', "values": "1x20"},
             {"series": 'shc_fabric_egress6_node_healthy{namespace="sovereign-cloud",name="lab",node="bgw-2"}', "values": "0x20"},
             {"series": 'shc_fabric_pool_blocks_free{namespace="sovereign-cloud",name="lab",pool="p6"}', "values": "3x90"},
             {"series": 'shc_fabric_pool_blocks_used{namespace="sovereign-cloud",name="lab",pool="p6"}', "values": "30x90"},
             {"series": 'shc_fabric_pool_blocks_held_down{namespace="sovereign-cloud",name="lab",pool="p6"}', "values": "7x90"},
         ],
         "alert_rule_test": [
             {"eval_time": "1m", "alertname": "ShcFabricNodeUnhealthy", "exp_alerts": []},
             {"eval_time": "3m", "alertname": "ShcFabricNodeUnhealthy", "exp_alerts": [
                 {"exp_labels": {"severity": "critical", "customresource_kind": "HybridFabric",
                                 "namespace": "sovereign-cloud", "name": "lab", "reason": "StartFailed"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: StartFailed on a border gateway node",
                      "description": "The fabric health check found StartFailed. status.health names the node and the "
                                     "finding; a full run was requested where one repairs it."}}]},
             {"eval_time": "11m", "alertname": "ShcFabricSiteSessionsDown", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": "sovereign-cloud", "name": "edge1"},
                  "exp_annotations": {
                      "summary": "CloudGateway sovereign-cloud/edge1: no routing session with its site",
                      "description": "No node holds a session with the site. A site that is off on purpose is paused "
                                     "(CloudInfrastructure spec.paused), which this alert ignores."}}]},
             {"eval_time": "6m", "alertname": "ShcFabricIPv6EgressUpstreamDown", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": "sovereign-cloud", "name": "lab"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: a node has no IPv6 upstream",
                      "description": "A node has no IPv6 upstream session or default route; the networks' IPv6 "
                                     "internet traffic takes the other node(s)."}}]},
             {"eval_time": "6m", "alertname": "ShcFabricIPv6EgressDown", "exp_alerts": []},
             {"eval_time": "65m", "alertname": "ShcPlatformPoolLow", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": "sovereign-cloud", "name": "lab", "pool": "p6"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: platform address pool p6 below 10 % free",
                      "description": "Few free blocks are left in the pool (blocks in hold-down count as not free). "
                                     "Add prefixes to the pool or shorten its hold-down."}}]},
         ]},
        {"interval": "1m",
         "input_series": [
             {"series": 'shc_fabric_info{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="lab",phase="Ready"}',
              "values": "1x120"},
             # checked at t=0 (Unix 0), interval 600 s: stale past 1800 s plus the rule's 10 minutes
             {"series": 'shc_fabric_health_checked_at{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="lab"}',
              "values": "0x120"},
             {"series": 'shc_fabric_health_interval_seconds{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="lab"}',
              "values": "600x120"},
             {"series": 'shc_fabric_info{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="new",phase="Ready"}',
              "values": "1x120"},
             {"series": 'shc_fabric_egress6_node_healthy{namespace="sovereign-cloud",name="lab",node="bgw-1"}', "values": "0x20"},
             {"series": 'shc_fabric_egress6_node_healthy{namespace="sovereign-cloud",name="lab",node="bgw-2"}', "values": "0x20"},
         ],
         "alert_rule_test": [
             {"eval_time": "35m", "alertname": "ShcFabricHealthStale", "exp_alerts": []},
             {"eval_time": "41m", "alertname": "ShcFabricHealthStale", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": "sovereign-cloud", "name": "lab"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: the fabric health check has not read the nodes for 3 intervals",
                      "description": "Check the fabric-health job template's schedule and its last runs; "
                                     "status.health.skipped says when a change to the nodes kept it away."}}]},
             {"eval_time": "61m", "alertname": "ShcFabricHealthNeverChecked", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "customresource_kind": "HybridFabric",
                                 "namespace": "sovereign-cloud", "name": "new", "phase": "Ready"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/new: the fabric health check never ran",
                      "description": "The fabric-health job template has no schedule, or its runs fail before they "
                                     "read the nodes."}}]},
             {"eval_time": "6m", "alertname": "ShcFabricIPv6EgressDown", "exp_alerts": [
                 {"exp_labels": {"severity": "critical", "namespace": "sovereign-cloud", "name": "lab"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: no node has an IPv6 upstream",
                      "description": "IPv6 internet egress is down for every network of the fabric."}}]},
         ]},
    ],
}


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class Chart(unittest.TestCase):
    def test_off_renders_nothing_of_it(self):
        docs = helm()
        names = [d["metadata"]["name"] for d in docs]
        self.assertNotIn("shc-fabric-metrics", names)
        self.assertNotIn("shc-fabric", names)

    def test_on(self):
        docs = helm("--set", "ops.fabricHealth.enabled=true", "--set", "ops.fabricHealth.image=example/ksm:1")
        by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
        dep = by[("Deployment", "shc-fabric-metrics")]
        c = dep["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(c["image"], "example/ksm:1")
        self.assertIn("--custom-resource-state-only", c["args"])
        cfg = yaml.safe_load(by[("ConfigMap", "shc-fabric-metrics")]["data"]["config.yaml"])
        kinds = [r["groupVersionKind"]["kind"] for r in cfg["spec"]["resources"]]
        self.assertEqual(kinds, ["HybridFabric", "CloudGateway"])
        rules = by[("PrometheusRule", "shc-fabric")]["spec"]["groups"][0]["rules"]
        self.assertIn("ShcFabricHealthStale", [r["alert"] for r in rules])
        role = by[("ClusterRole", "shc-ops-fabric-metrics")]
        self.assertEqual(role["rules"][0]["verbs"], ["get", "list", "watch"])

    def test_promtool(self):
        tool = promtool()
        if tool is None:
            self.skipTest("neither promtool nor the Prometheus image")
        docs = helm("--set", "ops.fabricHealth.enabled=true")
        groups = next(d for d in docs if d["kind"] == "PrometheusRule")["spec"]["groups"]
        tmp = tempfile.mkdtemp(prefix="shc-promtool-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "rules.yml"), "w") as f:
            yaml.safe_dump({"groups": groups}, f)
        with open(os.path.join(tmp, "test.yml"), "w") as f:
            yaml.safe_dump(TESTS, f)
        os.chmod(tmp, 0o755)
        for args in (("check", "rules", "rules.yml"), ("test", "rules", "test.yml")):
            p = subprocess.run(tool(tmp, *args), cwd=tmp, capture_output=True, text=True, timeout=300)
            self.assertEqual(p.returncode, 0, "promtool %s: %s %s" % (" ".join(args), p.stdout, p.stderr))


if __name__ == "__main__":
    unittest.main()
