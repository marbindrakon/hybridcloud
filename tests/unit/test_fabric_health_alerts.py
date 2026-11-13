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


def node_row(kind, name, node, reason, message, ns="sovereign-cloud"):
    return ('shc_fabric_node_reachable{customresource_kind="%s",namespace="%s",name="%s",node="%s",reason="%s",message="%s"}'
            % (kind, ns, name, node, reason, message))


def cond(kind, name, type_, status, reason, ns="sovereign-cloud"):
    return ('shc_fabric_condition{customresource_kind="%s",namespace="%s",name="%s",type="%s",status="%s",reason="%s"}'
            % (kind, ns, name, type_, status, reason))


TESTS = {
    "rule_files": ["rules.yml"],
    "evaluation_interval": "1m",
    "tests": [
        {"interval": "1m",
         "input_series": [
             {"series": node_row("HybridFabric", "lab", "bgw-2", "StartFailed",
                                 "the configuration is not applied on the node (failed, exit-code): gate H9 injected failure"),
              "values": "1x20"},
             {"series": node_row("HybridFabric", "lab", "bgw-1", "", ""), "values": "1x20"},
             {"series": node_row("CloudGateway", "iaas1", "bgw-2", "NodeUnreachable", "x"), "values": "0x20"},
             {"series": node_row("CloudGateway", "edge1", "bgw-1", "SiteSessionsDown", "no session"), "values": "1x20"},
             {"series": node_row("HybridFabric", "lab", "bgw-1", "ServiceSessionsDown", "no platform circuit session"),
              "values": "_x2 1x18"},
             {"series": node_row("HybridFabric", "lab", "bgw-2", "ServiceLinkDown", "the service link of acme/n1 has no session"),
              "values": "_x2 1x18"},
             {"series": cond("HybridFabric", "lab", "IPv6EgressUpstream", "False", "Degraded"), "values": "1000x20"},
             # Review R4 NM2: a wrong service setting (the nodes keep the last applied service)
             {"series": cond("HybridFabric", "lab", "ServiceRouting", "False", "InvalidSpec"), "values": "1000x20"},
             {"series": cond("HybridFabric", "lab2", "ServiceRouting", "True", "Ready"), "values": "1000x20"},
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
                                 "namespace": "sovereign-cloud", "name": "lab", "reason": "StartFailed", "node": "bgw-2",
                                 "message": "the configuration is not applied on the node (failed, exit-code): gate H9 injected failure"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: StartFailed on bgw-2",
                      "description": "the configuration is not applied on the node (failed, exit-code): gate H9 "
                                     "injected failure. The fabric health check requested a full run where one "
                                     "repairs it; status.health has every finding."}}]},
             {"eval_time": "11m", "alertname": "ShcFabricSiteSessionsDown", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": "sovereign-cloud", "name": "edge1", "node": "bgw-1"},
                  "exp_annotations": {
                      "summary": "CloudGateway sovereign-cloud/edge1: no routing session with its site on bgw-1",
                      "description": "The node holds no session with the site. A site that is off on purpose is paused "
                                     "(CloudInfrastructure spec.paused), which this alert ignores."}}]},
             {"eval_time": "9m", "alertname": "ShcFabricServiceSessionsDown", "exp_alerts": []},
             {"eval_time": "13m", "alertname": "ShcFabricServiceSessionsDown", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": "sovereign-cloud", "name": "lab", "node": "bgw-1"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: no session with the hub's router on bgw-1",
                      "description": "The node has a platform circuit (AttachmentCircuit ceKind platform) but no "
                                     "established BGP session on it; the platform reaches the clusters on tenant "
                                     "networks through the other nodes, or not at all when every node reports it."}}]},
             {"eval_time": "9m", "alertname": "ShcFabricServiceLinkDown", "exp_alerts": []},
             {"eval_time": "13m", "alertname": "ShcFabricServiceLinkDown", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": "sovereign-cloud", "name": "lab", "node": "bgw-2"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: a network's session with the platform's connection is down on bgw-2",
                      "description": "The node's platform circuit is up but the session over a network's service link "
                                     "is not, so that network's VRF on the node does not learn the hub's ranges; the "
                                     "network reaches the hub through the other nodes, or not at all when every node "
                                     "reports it. status.health names the network."}}]},
             {"eval_time": "6m", "alertname": "ShcFabricIPv6EgressUpstreamDown", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": "sovereign-cloud", "name": "lab"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: a node has no IPv6 upstream",
                      "description": "A node has no IPv6 upstream session or default route; the networks' IPv6 "
                                     "internet traffic takes the other node(s)."}}]},
             {"eval_time": "6m", "alertname": "ShcFabricIPv6EgressDown", "exp_alerts": []},
             {"eval_time": "6m", "alertname": "ShcFabricServiceSettingInvalid", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": "sovereign-cloud", "name": "lab"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: a setting of the platform's connection for clusters on networks is wrong",
                      "description": "The fabric keeps the connection as last applied on its nodes (or, never applied, does "
                                     "not realize it) until the setting is fixed: no new cluster address is carried. The "
                                     "condition ServiceRouting names the setting."}}]},
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
             # ran and read at t=0 (Unix 0), no run since, interval 600 s: stale past 1800 s plus the rule's 10 minutes
             {"series": 'shc_fabric_health_checked_at{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="lab"}',
              "values": "0x120"},
             {"series": 'shc_fabric_health_last_run_at{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="lab"}',
              "values": "0x120"},
             {"series": 'shc_fabric_health_interval_seconds{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="lab"}',
              "values": "600x120"},
             {"series": 'shc_fabric_info{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="new",phase="Ready"}',
              "values": "1x120"},
             # a fabric whose check the realization turned off: neither stale nor never checked
             {"series": 'shc_fabric_info{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="off",phase="Ready"}',
              "values": "1x120"},
             {"series": 'shc_fabric_health_enabled{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="off"}',
              "values": "0x120"},
             {"series": 'shc_fabric_egress6_node_healthy{namespace="sovereign-cloud",name="lab",node="bgw-1"}', "values": "0x20"},
             {"series": 'shc_fabric_egress6_node_healthy{namespace="sovereign-cloud",name="lab",node="bgw-2"}', "values": "0x20"},
         ],
         "alert_rule_test": [
             {"eval_time": "35m", "alertname": "ShcFabricHealthStale", "exp_alerts": []},
             {"eval_time": "41m", "alertname": "ShcFabricHealthStale", "exp_alerts": [
                 {"exp_labels": {"severity": "warning", "namespace": "sovereign-cloud", "name": "lab"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/lab: no fabric health run for 41m 0s",
                      "description": "The fabric health check has not run for 41m 0s (more than 3 intervals). Check "
                                     "the fabric-health job template's schedule and its last runs."}}]},
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
        {"interval": "1m",
         "input_series": [
             # runs every 10 min (lastRunAt follows time: value = the minute's Unix seconds), the nodes last read at
             # t=0 because jobs held the lease from then on
             {"series": 'shc_fabric_health_last_run_at{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="busy"}',
              "values": "0+60x180"},
             {"series": 'shc_fabric_health_checked_at{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="busy"}',
              "values": "0x180"},
             {"series": 'shc_fabric_health_interval_seconds{customresource_kind="HybridFabric",namespace="sovereign-cloud",name="busy"}',
              "values": "600x180"},
         ],
         "alert_rule_test": [
             {"eval_time": "60m", "alertname": "ShcFabricHealthStale", "exp_alerts": []},
             {"eval_time": "60m", "alertname": "ShcFabricNodesNotRead", "exp_alerts": []},
             {"eval_time": "120m", "alertname": "ShcFabricHealthStale", "exp_alerts": []},
             {"eval_time": "99m", "alertname": "ShcFabricNodesNotRead", "exp_alerts": []},
             {"eval_time": "101m", "alertname": "ShcFabricNodesNotRead", "exp_alerts": [
                 {"exp_labels": {"severity": "info", "namespace": "sovereign-cloud", "name": "busy"},
                  "exp_annotations": {
                      "summary": "HybridFabric sovereign-cloud/busy: the nodes have not been health-checked for 1h 41m 0s",
                      "description": "The health check runs but leaves the nodes alone while a change to them holds the "
                                     "configuration lease (status.health.skipped). Expected during a long rollout; "
                                     "otherwise check which job holds the lease fabric-config-<fabric>."}}]},
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
        self.assertTrue(by[("ServiceMonitor", "shc-fabric-metrics")]["spec"]["endpoints"][0]["honorLabels"])
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
