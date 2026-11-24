"""The route reflector sessions a placement's RouteAdvertisements select (shc-cluster-base templates/fabric.yaml,
networkplacement_provision backend_cloudvirt.yml): the chart's FRRConfigurations carry the product label key and, for
one release, the key of the release before, so every selector a realization can name selects them: the default
(empty: every FRRConfiguration), the product key, and the earlier key. The RouteAdvertisements is written whole (a
replace with the resourceVersion the site read): its selector is exactly the realization's, never merged with the
selector an earlier run wrote. The chart is rendered with helm (skipped when helm is absent), the definition in
ansible-playbook (skipped when it is absent). Names are examples."""
import os
import shutil
import subprocess
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

CHART = os.path.join(ROOT, "charts", "charts", "shc-cluster-base")
TASKS = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_provision", "tasks", "backend_cloudvirt.yml")
RA_TASK = "RouteAdvertisements {{ np_names.routeAdvertisement }}"
PRODUCT = {"hybridsovereign.redhat/fabric": "route-reflectors"}
# the key the release before wrote (and that an installation's realization may still name for one release)
EARLIER = {"shc.signal9.gg/fabric": "route-reflectors"}
SELECTORS = {"default": {}, "product key": PRODUCT, "earlier key": EARLIER}


def chart_labels():
    p = subprocess.run(["helm", "template", "t", CHART, "--show-only", "templates/fabric.yaml",
                        "--set", "layers.base=false", "--set", "layers.certs=true", "--set", "fabric.enabled=true",
                        "--set", "fabric.nodes[0].name=n1", "--set", "fabric.nodes[0].vtepIP=192.0.2.1",
                        "--set", "letsencrypt.enabled=false", "--set", "cluster.name=c1",
                        "--set", "cluster.baseDomain=example.test"],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise AssertionError(p.stderr)
    frr = [d for d in yaml.safe_load_all(p.stdout) if d and d.get("kind") == "FRRConfiguration"]
    assert frr, "the chart renders no FRRConfiguration"
    return [d["metadata"].get("labels") or {} for d in frr]


def ra_task():
    return by_name(load(TASKS), RA_TASK)


def render(selector, existing):
    t = ra_task()
    task = {"name": "render", "vars": t.get("vars", {}),
            "ansible.builtin.set_fact": {"_ra": t["kubernetes.core.k8s"]["definition"]}}
    vars_ = {"np_names": {"routeAdvertisement": "hn51001", "labels": {"hybridsovereign.redhat/l3vni": "51001"},
                          "annotations": {"hybridsovereign.redhat/names": "{}"}},
             "np_real": {"l3Vni": 51001},
             "fab_real": {"openshift": {"frrConfigurationSelector": selector}},
             "_np_site_existing": {"results": [{"resources": []}, {"resources": existing}]}}
    return run_play([task, write_out("_ra")], vars_)


def matches(selector, labels):
    return all(labels.get(k) == v for k, v in selector.items())


class Wiring(unittest.TestCase):
    def test_the_route_advertisement_is_replaced_not_merged(self):
        k = ra_task()["kubernetes.core.k8s"]
        self.assertIs(k.get("force"), True)
        self.assertNotIn("apply", k)
        self.assertNotIn("merge_type", k)


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class ChartLabels(unittest.TestCase):
    def test_every_selector_selects_every_route_reflector_configuration(self):
        for labels in chart_labels():
            for what, sel in SELECTORS.items():
                with self.subTest(selector=what):
                    self.assertTrue(matches(sel, labels), f"{sel} does not select {labels}")


@unittest.skipUnless(HAVE_ANSIBLE and shutil.which("helm"), "ansible-playbook or helm not installed")
class SelectorRendered(unittest.TestCase):
    """What the job writes selects what the chart labels, and nothing else is left in the selector."""

    def test_the_selector_is_exactly_the_realizations(self):
        labels = chart_labels()
        earlier_ra = {"metadata": {"name": "hn51001", "resourceVersion": "42"},
                      "spec": {"frrConfigurationSelector": {"matchLabels": EARLIER}}}
        for what, sel in SELECTORS.items():
            for existing in ([], [earlier_ra]):
                with self.subTest(selector=what, existing=bool(existing)):
                    ra = render(sel, existing)
                    got = ra["spec"]["frrConfigurationSelector"]["matchLabels"]
                    self.assertEqual(got, sel)
                    self.assertTrue(all(matches(got, lb) for lb in labels))
                    if existing:
                        self.assertEqual(ra["metadata"]["resourceVersion"], "42")
                    else:
                        self.assertNotIn("resourceVersion", ra["metadata"])
                    self.assertEqual(ra["spec"]["networkSelectors"][0]["clusterUserDefinedNetworkSelector"]
                                     ["networkSelector"]["matchLabels"], {"hybridsovereign.redhat/l3vni": "51001"})


if __name__ == "__main__":
    unittest.main()
