"""The mce-cluster-build chart's AgentClusterInstall with and without per-family networking (provider.agent.networking,
cluster.apiVIPs / cluster.ingressVIPs).

- Single-stack values (machineCIDR, apiVIP / ingressVIP, no networking): the chart renders byte for byte what the
  release before per-family networking rendered (BASE below, read from Git; without it that comparison is skipped), and
  the single-stack defaults (10.128.0.0/14 hostPrefix 23, 172.30.0.0/16, machineCIDR).
- Dual-stack values: the three lists as given (IPv4 first), apiVIPs / ingressVIPs in place of the single VIPs, a list
  left out keeps its default, and an external load balancer still renders no VIP at all.

Needs helm (skipped without it). Documentation ranges only."""
import copy
import difflib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import ROOT  # noqa: E402

CHART = os.path.join(ROOT, "charts", "charts", "mce-cluster-build")
# the release before provider.agent.networking (0.2.1-shc.6)
BASE = "7c23801"
HAVE_HELM = shutil.which("helm") is not None

# an Agent (bare-metal) cluster with its own VIPs, the shape of the installation's build values
SINGLE = {
    "cluster": {"name": "site-vm", "baseDomain": "example.com", "clusterImageSet": "img4.22.16-x86-64-appsub",
                "controlPlaneCount": 3, "workerCount": 3, "apiVIP": "198.51.100.5", "ingressVIP": "198.51.100.7",
                "pullSecretVault": "clusters/site-vm/pull-secret", "pullSecretVaultProperty": ".dockerconfigjson",
                "sshKeyVault": "clusters/site-vm/ssh-key", "sshPubKey": "ssh-ed25519 AAAAexample site-vm",
                "extraManifests": {"cluster-network-03-config.yaml": {
                    "apiVersion": "operator.openshift.io/v1", "kind": "Network", "metadata": {"name": "cluster"},
                    "spec": {"defaultNetwork": {"ovnKubernetesConfig": {"gatewayConfig": {"routingViaHost": True}}}}}}},
    "provider": {"type": "Agent",
                 "agent": {"infraEnvType": "shared", "sharedInfraEnv": {"namespace": "metal-infra-env", "name": "metal-infra-env"},
                           "platformType": "BareMetal", "useExternalLB": False, "machineCIDR": "198.51.100.0/24"}},
    "secretStoreName": "vault-backend",
}
# the same cluster behind an external load balancer (no VIPs, user-managed networking)
EXTERNAL_LB = copy.deepcopy(SINGLE)
EXTERNAL_LB["provider"]["agent"].update(useExternalLB=True, platformType=None)
EXTERNAL_LB["cluster"].update(apiVIP="", ingressVIP="")

NETWORKING = {"clusterNetwork": [{"cidr": "10.128.0.0/14", "hostPrefix": 23}, {"cidr": "fd01::/48", "hostPrefix": 64}],
              "serviceNetwork": ["172.30.0.0/16", "fd02::/112"],
              "machineNetwork": [{"cidr": "198.51.100.0/24"}, {"cidr": "2001:db8:1:238::/64"}]}
VIPS6 = {"apiVIPs": ["198.51.100.5", "2001:db8:1:238::5"], "ingressVIPs": ["198.51.100.7", "2001:db8:1:238::7"]}

DEFAULT_NETWORKING = {"clusterNetwork": [{"cidr": "10.128.0.0/14", "hostPrefix": 23}],
                      "machineNetwork": [{"cidr": "198.51.100.0/24"}], "serviceNetwork": ["172.30.0.0/16"],
                      "userManagedNetworking": False}


def with_values(base, cluster=None, agent=None):
    v = copy.deepcopy(base)
    v["cluster"].update(cluster or {})
    v["provider"]["agent"].update(agent or {})
    return v


def render(chart, values):
    """helm template of the chart with these values, as text."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
    try:
        p = subprocess.run(["helm", "template", values["cluster"]["name"], chart, "-n", values["cluster"]["name"], "-f", f.name],
                           capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(f.name)
    if p.returncode != 0:
        raise AssertionError(p.stderr[-2000:])
    return p.stdout


def aci(text):
    """The AgentClusterInstall of a rendered chart."""
    docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    return next(d for d in docs if d.get("kind") == "AgentClusterInstall")


def base_chart(tmp):
    """The chart as the release before had it, unpacked from Git; None without the commit."""
    try:
        p = subprocess.run(["git", "-C", ROOT, "archive", BASE, os.path.relpath(CHART, ROOT)],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tar:
        tar.extractall(tmp, filter="data")
    return os.path.join(tmp, os.path.relpath(CHART, ROOT))


@unittest.skipUnless(HAVE_HELM, "helm not installed")
class SingleStack(unittest.TestCase):
    def test_unchanged_from_the_release_before(self):
        tmp = tempfile.mkdtemp(prefix="shc-mce-base-")
        try:
            base = base_chart(tmp)
            if base is None:
                self.skipTest("the release before (%s) is not in this Git checkout" % BASE)
            for values in (SINGLE, EXTERNAL_LB):
                before, after = render(base, values), render(CHART, values)
                if before != after:
                    self.fail("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "before", "after")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_defaults(self):
        spec = aci(render(CHART, SINGLE))["spec"]
        self.assertEqual(spec["networking"], DEFAULT_NETWORKING)
        self.assertEqual((spec["apiVIP"], spec["ingressVIP"]), ("198.51.100.5", "198.51.100.7"))
        self.assertNotIn("apiVIPs", spec)
        self.assertNotIn("ingressVIPs", spec)

    def test_external_lb(self):
        spec = aci(render(CHART, EXTERNAL_LB))["spec"]
        self.assertEqual(spec["networking"], dict(DEFAULT_NETWORKING, userManagedNetworking=True))
        for key in ("apiVIP", "ingressVIP", "apiVIPs", "ingressVIPs", "platformType"):
            self.assertNotIn(key, spec)


@unittest.skipUnless(HAVE_HELM, "helm not installed")
class DualStack(unittest.TestCase):
    def test_lists_and_vips(self):
        text = render(CHART, with_values(SINGLE, cluster=VIPS6, agent={"networking": NETWORKING}))
        spec = aci(text)["spec"]
        self.assertEqual(spec["networking"], dict(NETWORKING, userManagedNetworking=False))
        self.assertEqual((spec["apiVIPs"], spec["ingressVIPs"]), (VIPS6["apiVIPs"], VIPS6["ingressVIPs"]))
        self.assertNotIn("apiVIP", spec)
        self.assertNotIn("ingressVIP", spec)
        # IPv4 first, as given
        self.assertLess(text.index("cidr: 10.128.0.0/14"), text.index("cidr: fd01::/48"))
        self.assertLess(text.index("- 172.30.0.0/16"), text.index("- fd02::/112"))

    def test_networks_without_vip_lists(self):
        """dual-stack networks with IPv4-only VIPs: the single VIPs stay"""
        spec = aci(render(CHART, with_values(SINGLE, agent={"networking": NETWORKING})))["spec"]
        self.assertEqual(spec["networking"]["machineNetwork"], NETWORKING["machineNetwork"])
        self.assertEqual((spec["apiVIP"], spec["ingressVIP"]), ("198.51.100.5", "198.51.100.7"))
        self.assertNotIn("apiVIPs", spec)

    def test_partial_networking_keeps_the_other_defaults(self):
        spec = aci(render(CHART, with_values(SINGLE, agent={"networking": {"serviceNetwork": NETWORKING["serviceNetwork"]}})))["spec"]
        self.assertEqual(spec["networking"], dict(DEFAULT_NETWORKING, serviceNetwork=NETWORKING["serviceNetwork"]))

    def test_external_lb_renders_no_vip(self):
        spec = aci(render(CHART, with_values(EXTERNAL_LB, cluster=VIPS6, agent={"networking": NETWORKING})))["spec"]
        self.assertEqual(spec["networking"], dict(NETWORKING, userManagedNetworking=True))
        for key in ("apiVIP", "ingressVIP", "apiVIPs", "ingressVIPs"):
            self.assertNotIn(key, spec)


if __name__ == "__main__":
    unittest.main()
