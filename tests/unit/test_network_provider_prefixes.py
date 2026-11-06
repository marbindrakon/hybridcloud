"""The HybridNetwork's status.ipv6.providerPrefixes: the network job lists the IPv6 subnets cloud providers assigned
to its placements (their status.subnets rows with source Provider) as {prefix, placement, site}, read-only next to the
address space; absent when there is none. A ready placement whose provider subnet came, went or changed since the
network listed it wakes the network, and so does every completed placement deletion (except of a network being
deleted). The row shapes follow the
recorded gate evidence: an AWS placement reports the Amazon /56 (dual-stack and IPv6-only alike; the per-zone /64s
stay in status.realization.site.ipv6.subnets), a Google Cloud placement the subnet's external /64. The plays run in
ansible-playbook (skipped without it); the wiring checks always run. Documentation prefixes only."""
import copy
import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign",
                                "shc", "plugins", "module_utils"))
import shc_status as S  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
HN = os.path.join(ROLES, "hybridnetwork_provision", "tasks")
NP = os.path.join(ROLES, "networkplacement_provision", "tasks")
NPD = os.path.join(ROLES, "networkplacement_teardown", "tasks")
CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-hybridnetwork.yaml")

AWS56 = "2001:db8:0:5800::/56"          # what an AWS placement reports (the VPC's Amazon block)
AWS56_B = "2001:db8:0:7700::/56"
GCP64 = "2001:db8:4090:c74::/64"        # what a Google Cloud placement reports (the subnet's external /64)
NET64 = "2001:db8:4005:4900::/64"       # a placement subnet from the network's own space


def row(prefix, source):
    return {"family": 6 if ":" in prefix else 4, "prefix": prefix, "source": source}


def pl(name, subnets, network="net", site="site-a", deleting=False, complete=False):
    md = {"name": name, "namespace": "entity-a"}
    if deleting or complete:
        md["deletionTimestamp"] = "2001-01-01T00:00:00Z"
    st = {"subnets": subnets}
    if site:
        st["site"] = site
    if complete:
        st["deletionComplete"] = True
    return {"metadata": md, "spec": {"network": network}, "status": st}


AWS_DUAL = pl("web-aws", [row("10.70.1.0/24", "Network"), row(AWS56, "Provider")], site="aws-1")
AWS_ONLY = pl("v6-aws", [row(AWS56_B, "Provider")], site="aws-1")
GCP_DUAL = pl("web-gcp", [row("10.70.2.0/24", "Network"), row(GCP64, "Provider")], site="gcp-1")
METAL = pl("web-vms", [row("10.70.0.0/24", "Network"), row(NET64, "Network")], site="metal-1")


def nudged(task, extra):
    """Run one nudge task with its include replaced by a flag: did it wake anything?"""
    t = copy.deepcopy(task)
    t.pop("ansible.builtin.include_tasks")
    t["ansible.builtin.set_fact"] = {"woke": True}
    return run_play([t, write_out("woke | default(false)")], extra)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class ProviderPrefixList(unittest.TestCase):
    def listed(self, placements, name="net"):
        t = by_name(load(os.path.join(HN, "main.yml")), "The placements' provider-assigned IPv6 subnets")
        return run_play([t, write_out("hn_provider6")], {"fab_name": name, "_hn_placements": {"resources": placements}})

    def test_aws_dual_stack_lists_the_amazon_block_only(self):
        self.assertEqual(self.listed([AWS_DUAL]), [{"prefix": AWS56, "placement": "web-aws", "site": "aws-1"}])

    def test_aws_ipv6_only_lists_the_amazon_block(self):
        self.assertEqual(self.listed([AWS_ONLY]), [{"prefix": AWS56_B, "placement": "v6-aws", "site": "aws-1"}])

    def test_gcp_lists_the_external_64(self):
        self.assertEqual(self.listed([GCP_DUAL]), [{"prefix": GCP64, "placement": "web-gcp", "site": "gcp-1"}])
        only = pl("v6-gcp", [row(GCP64, "Provider")], site="gcp-1")
        self.assertEqual(self.listed([only]), [{"prefix": GCP64, "placement": "v6-gcp", "site": "gcp-1"}])

    def test_mixed_with_network_sourced_and_other_networks(self):
        other = pl("x-aws", [row("2001:db8:0:9900::/56", "Provider")], network="other", site="aws-1")
        out = self.listed([METAL, GCP_DUAL, other, AWS_ONLY, AWS_DUAL])
        self.assertEqual(out, [{"prefix": AWS56_B, "placement": "v6-aws", "site": "aws-1"},
                               {"prefix": AWS56, "placement": "web-aws", "site": "aws-1"},
                               {"prefix": GCP64, "placement": "web-gcp", "site": "gcp-1"}])

    def test_the_order_does_not_follow_the_listing(self):
        pls = [METAL, GCP_DUAL, AWS_ONLY, AWS_DUAL]
        outs = [self.listed(list(p)) for p in itertools.islice(itertools.permutations(pls), 0, 24, 5)]
        for o in outs[1:]:
            self.assertEqual(o, outs[0])

    def test_no_ipv4_and_no_network_rows(self):
        odd = pl("odd", [row("192.0.2.0/24", "Provider"), {"family": 6, "prefix": "2001:db8:0:1::/64"},
                         row(NET64, "Network")])
        self.assertEqual(self.listed([odd, METAL]), [])

    def test_nothing_without_provider_subnets(self):
        self.assertEqual(self.listed([]), [])
        self.assertEqual(self.listed([METAL]), [])
        self.assertEqual(self.listed([pl("bare", [])]), [])
        self.assertEqual(self.listed([{"metadata": {"name": "new"}, "spec": {"network": "net"}}]), [])

    def test_a_placement_counts_until_its_teardown_is_complete(self):
        tearing = pl("web-aws", AWS_DUAL["status"]["subnets"], site="aws-1", deleting=True)
        self.assertEqual(self.listed([tearing]), [{"prefix": AWS56, "placement": "web-aws", "site": "aws-1"}])
        done = pl("web-aws", AWS_DUAL["status"]["subnets"], site="aws-1", complete=True)
        self.assertEqual(self.listed([done, GCP_DUAL]), [{"prefix": GCP64, "placement": "web-gcp", "site": "gcp-1"}])
        # a placement that is gone is simply not listed
        self.assertEqual(self.listed([GCP_DUAL]), [{"prefix": GCP64, "placement": "web-gcp", "site": "gcp-1"}])

    def test_an_entry_without_a_known_site_has_no_site(self):
        self.assertEqual(self.listed([pl("p", [row(GCP64, "Provider")], site="")]), [{"prefix": GCP64, "placement": "p"}])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class ProviderPrefixStatus(unittest.TestCase):
    def ipv6_status(self, provider6):
        t = by_name(load(os.path.join(HN, "main.yml")), "Keep the address space")
        return run_play([t, write_out("hn_ipv6_status")],
                        {"fab_ipam_result": {"addressSpace": [], "changed": False}, "hn_block": "2001:db8:4005:4900::/56",
                         "hn_tenant6": [], "hn_platform6": {"family": 6}, "hn_mode6": "Platform", "hn_provider6": provider6})

    def test_absent_when_empty_like_block_and_hold_down(self):
        self.assertIsNone(self.ipv6_status([])["providerPrefixes"])

    def test_the_list_as_computed(self):
        lst = [{"prefix": AWS56, "placement": "web-aws", "site": "aws-1"}]
        self.assertEqual(self.ipv6_status(lst)["providerPrefixes"], lst)

    def test_the_wait_for_platform_space_writes_it_too(self):
        t = by_name(load(os.path.join(HN, "platform_block.yml")), "Report the network waiting for platform address space")
        expr = t["vars"]["fab_status"]
        tasks = [{"ansible.builtin.set_fact": {"st": expr}}, write_out("st.ipv6")]
        base = {"hn_status": {"entity": "a"}, "fab_ipam_result": {"waiting": "x"}}
        self.assertIsNone(run_play(tasks, dict(base, hn_provider6=[]))["providerPrefixes"])
        lst = [{"prefix": GCP64, "placement": "web-gcp", "site": "gcp-1"}]
        self.assertEqual(run_play(tasks, dict(base, hn_provider6=lst))["providerPrefixes"], lst)


class ProviderPrefixMerge(unittest.TestCase):
    """Every status write is a JSON merge patch: the job's null removes the list, a shorter list replaces it, and a
    write that carries only status.ipv6.providerPrefixes leaves the other keys of status.ipv6 as they are. (The status
    schema keeps unknown fields at its top level only; status.ipv6 does not, so without the CRD field the API server
    prunes the list.)"""

    def obj(self, lst):
        return {"metadata": {"resourceVersion": "1", "generation": 1},
                "status": {"status": "ready", "ipv6": {"mode": "Platform", "providerPrefixes": lst}}}

    def write(self, before, after):
        o = self.obj(before)
        patch, _ = S.compute_status_patch(o, {"status": "ready", "ready": True, "ipv6": {"mode": "Platform",
                                                                                      "providerPrefixes": after}})
        return S._merge_patch(o["status"], patch["status"])["ipv6"]

    def test_null_removes_the_list(self):
        two = [{"prefix": AWS56, "placement": "a"}, {"prefix": GCP64, "placement": "b"}]
        self.assertNotIn("providerPrefixes", self.write(two, None))

    def test_a_shorter_list_replaces_the_longer(self):
        two = [{"prefix": AWS56, "placement": "a"}, {"prefix": GCP64, "placement": "b"}]
        self.assertEqual(self.write(two, two[1:])["providerPrefixes"], two[1:])

    FULL = {"mode": "Platform", "prefixes": ["2001:db8:4005:4900::/56"],
            "block": {"prefix": "2001:db8:4005:4900::/56", "prefixLength": 56, "source": "Allocated"}}

    def written(self, body, ipv6_before):
        o = {"metadata": {"resourceVersion": "1", "generation": 1},
             "status": {"status": "ready", "ready": True} | ({"ipv6": ipv6_before} if ipv6_before is not None else {})}
        patch, _ = S.compute_status_patch(o, body)
        return S._merge_patch(o["status"], patch["status"])

    def test_the_wait_for_the_fabric_removes_the_list_and_keeps_the_rest(self):
        before = dict(self.FULL, providerPrefixes=[{"prefix": AWS56, "placement": "a"}])
        body = {"status": "pending", "ready": False, "message": "Waiting for HybridFabric f (failed)",
                "waitingFor": ["HybridFabric f (failed)"], "placementCount": 0, "ipv6": {"providerPrefixes": None}}
        self.assertEqual(self.written(body, before)["ipv6"], self.FULL)

    def test_a_failure_replaces_the_list_and_keeps_the_rest(self):
        two = [{"prefix": AWS56, "placement": "a"}, {"prefix": GCP64, "placement": "b"}]
        body = {"status": "failed", "ready": False, "message": "HybridNetwork n: x", "ipv6": {"providerPrefixes": two[1:]}}
        self.assertEqual(self.written(body, dict(self.FULL, providerPrefixes=two))["ipv6"], dict(self.FULL, providerPrefixes=two[1:]))

    def test_a_write_without_the_patch_leaves_status_ipv6_alone(self):
        self.assertNotIn("ipv6", self.written({"status": "pending", "ready": False, "message": "m"}, None))
        self.assertEqual(self.written({"status": "failed", "ready": False, "message": "m"}, self.FULL)["ipv6"], self.FULL)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class ProviderPrefixOtherWrites(unittest.TestCase):
    """The wait for the fabric and a failure carry the list too (hn_provider6_patch), so it never goes stale there and
    a ready provider placement's comparison clears after one network run."""

    def patch(self, provider6, fab_st):
        t = by_name(load(os.path.join(HN, "main.yml")), "The provider-assigned subnets for the writes without the network's IPv6 status")
        return run_play([t, write_out("hn_provider6_patch")], {"hn_provider6": provider6, "fab_st": fab_st})

    def test_the_patch(self):
        lst = [{"prefix": AWS56, "placement": "web-aws", "site": "aws-1"}]
        with_ipv6 = {"ipv6": {"mode": "Platform"}}
        self.assertEqual(self.patch(lst, with_ipv6), {"ipv6": {"providerPrefixes": lst}})
        self.assertEqual(self.patch([], with_ipv6), {"ipv6": {"providerPrefixes": None}})
        self.assertEqual(self.patch(lst, {}), {"ipv6": {"providerPrefixes": lst}})
        # a network that has no status.ipv6 yet and nothing to list gets no ipv6 key at all
        self.assertEqual(self.patch([], {}), {})
        self.assertEqual(self.patch([], {"ipv6": None}), {})

    def expr(self, task_name, extra):
        t = by_name(load(os.path.join(HN, "main.yml")), task_name)
        return run_play([{"ansible.builtin.set_fact": {"st": t["vars"]["fab_status"]}}, write_out("st")], extra)

    def test_the_wait_for_the_fabric_carries_it(self):
        patch = {"ipv6": {"providerPrefixes": None}}
        st = self.expr("Report what the network waits for", {"hn_status": {"entity": "a", "placementCount": 1},
                                                           "hn_provider6_patch": patch})
        self.assertEqual(st, {"entity": "a", "placementCount": 1, "ipv6": {"providerPrefixes": None}})
        st = self.expr("Report what the network waits for", {"hn_status": {"entity": "a"}, "hn_provider6_patch": {}})
        self.assertEqual(st, {"entity": "a"})

    def test_a_failure_carries_it_when_the_job_got_that_far(self):
        lst = {"ipv6": {"providerPrefixes": [{"prefix": GCP64, "placement": "web-gcp", "site": "gcp-1"}]}}
        self.assertEqual(self.expr("Mark the HybridNetwork failed", {"hn_provider6_patch": lst}), lst)
        self.assertEqual(self.expr("Mark the HybridNetwork failed", {}), {})


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlacementWakesNetwork(unittest.TestCase):
    def woke(self, was, subnets, listed, name="web-aws"):
        t = by_name(load(os.path.join(NP, "main.yml")), "Tell the network when the placement's provider-assigned subnet changed")
        net = {"status": {"ipv6": {"mode": "Platform"} | ({"providerPrefixes": listed} if listed is not None else {})}}
        return nudged(t, {"fab_namespace": "entity-a", "fab_name": name, "fab_spec": {"network": "net"},
                          "fab_st": {"status": was}, "np_final": {"subnets": subnets}, "np_network": net})

    def test_listed_as_reported_wakes_nothing(self):
        listed = [{"prefix": AWS56, "placement": "web-aws", "site": "aws-1"},
                  {"prefix": GCP64, "placement": "web-gcp", "site": "gcp-1"}]
        self.assertFalse(self.woke("ready", AWS_DUAL["status"]["subnets"], listed))

    def test_a_network_without_the_list_is_woken(self):
        # e.g. the first run after the list was introduced
        self.assertTrue(self.woke("ready", AWS_DUAL["status"]["subnets"], None))

    def test_a_changed_subnet_wakes_the_network(self):
        listed = [{"prefix": AWS56_B, "placement": "web-aws", "site": "aws-1"}]
        self.assertTrue(self.woke("ready", AWS_DUAL["status"]["subnets"], listed))

    def test_a_subnet_that_went_wakes_the_network(self):
        listed = [{"prefix": AWS56, "placement": "web-aws", "site": "aws-1"}]
        self.assertTrue(self.woke("ready", [row("10.70.1.0/24", "Network")], listed))

    def test_network_sourced_placements_wake_nothing(self):
        listed = [{"prefix": GCP64, "placement": "web-gcp", "site": "gcp-1"}]
        self.assertFalse(self.woke("ready", METAL["status"]["subnets"], listed, name="web-vms"))
        self.assertFalse(self.woke("ready", METAL["status"]["subnets"], None, name="web-vms"))

    def test_a_placement_becoming_ready_is_left_to_the_wake_before(self):
        # "Tell the network and the backend" wakes the network then; no second wake
        self.assertFalse(self.woke("reconciling", AWS_DUAL["status"]["subnets"], None))
        self.assertFalse(self.woke("pending", AWS_DUAL["status"]["subnets"], None))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class TeardownWakesNetwork(unittest.TestCase):
    """Every completed placement deletion wakes the network (counts, provider list, the fabric's IPv6 announcements),
    except a network that is being deleted: there a wake would be a Retry now of its deletion."""

    def woke(self, network, fab_st=None, name="net"):
        tasks = copy.deepcopy(load(os.path.join(NPD, "wake_network.yml")))
        read = by_name(tasks, "Read the network of the deleted placement")
        read.pop("kubernetes.core.k8s_info")
        read.pop("register")
        read.pop("failed_when")
        # what the read would return: the network, nothing (gone), or a failed read (no resources)
        read["ansible.builtin.set_fact"] = {"_npd_wake_net": network}
        wake = by_name(tasks, "Wake the network of the deleted placement")
        wake.pop("ansible.builtin.include_tasks")
        wake["ansible.builtin.set_fact"] = {"woke": True}
        return run_play(tasks + [write_out("woke | default(false)")],
                        {"npd_wake_network": name, "npd_wake_namespace": "entity-a", "fab_st": fab_st or {}})

    LIVE = {"resources": [{"metadata": {"name": "net", "namespace": "entity-a"}, "status": {"ready": True}}]}
    DELETING = {"resources": [{"metadata": {"name": "net", "namespace": "entity-a",
                                            "deletionTimestamp": "2001-01-01T00:00:00Z"}}]}

    def test_every_placement_wakes_a_live_network(self):
        ipv4_only = pl("v4", [row("10.70.3.0/24", "Network")])
        for p in (METAL, AWS_DUAL, AWS_ONLY, GCP_DUAL, ipv4_only, pl("bare", [])):
            self.assertTrue(self.woke(self.LIVE, p["status"]), p["metadata"]["name"])

    def test_a_network_being_deleted_is_not_woken(self):
        self.assertFalse(self.woke(self.DELETING, METAL["status"]))

    def test_a_network_that_is_gone_or_unreadable_is_not_woken(self):
        self.assertFalse(self.woke({"resources": []}))
        self.assertFalse(self.woke({"failed": True, "msg": "connection refused"}))
        self.assertFalse(self.woke(self.LIVE, name=""))

    def test_the_short_path_runs_the_kind_s_steps_after_the_deletion(self):
        # teardown_unrealized.yml: a placement refused at validation is deleted without site cleanup; the hook runs
        t = by_name(load(os.path.join(ROOT, "eda", "common", "tasks", "teardown_unrealized.yml")),
                    "The kind's steps after the deletion")
        after = [{"ansible.builtin.set_fact": {"woke": True}}]
        self.assertTrue(run_play([t, write_out("woke | default(false)")], {"teardown_unrealized_after": "after.yml"},
                                 files={"after.yml": after}))
        self.assertFalse(run_play([t, write_out("woke | default(false)")], {}))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class CountsAfterADeletion(unittest.TestCase):
    def status(self, placements):
        t = by_name(load(os.path.join(HN, "main.yml")), "Set the network status")
        return run_play([t, write_out("{'status': hn_status, 'holders': hn_holders}")],
                        {"fab_name": "net", "fab_namespace": "entity-a", "fab_entity": "a", "fab_spec": {},
                         "hn_fabric": {}, "hn_fabric_name": "", "_hn_placements": {"resources": placements}})

    def test_a_completed_deletion_is_not_counted(self):
        ready = pl("web-vms", METAL["status"]["subnets"])
        ready["status"]["ready"] = True
        tearing = pl("web-aws", AWS_DUAL["status"]["subnets"], deleting=True)
        done = pl("old", METAL["status"]["subnets"], complete=True)
        out = self.status([ready, tearing, done])
        self.assertEqual((out["status"]["placementCount"], out["status"]["placementsReady"]), (2, 1))
        # the IPAM's holders still name every placement that exists
        self.assertEqual(len(out["holders"]), 3)


class Wiring(unittest.TestCase):
    def names(self, path):
        return [t.get("name") for t in walk(load(path))]

    def test_the_list_is_computed_before_any_status_write_that_carries_it(self):
        names = self.names(os.path.join(HN, "main.yml"))
        i = names.index("The placements' provider-assigned IPv6 subnets")
        j = names.index("The provider-assigned subnets for the writes without the network's IPv6 status")
        self.assertLess(names.index("Read the HybridNetwork"), names.index("List the network's placements"))
        self.assertLess(names.index("List the network's placements"), i)
        self.assertLess(i, j)
        # before the first refusal of the spec, so a failure carries it too
        self.assertLess(j, names.index("List the platform's fabrics"))
        self.assertLess(j, names.index("Refuse a fabric change"))
        self.assertLess(j, names.index("Report what the network waits for"))
        self.assertLess(i, names.index("The network's platform IPv6 block"))
        self.assertLess(i, names.index("Keep the address space"))
        with open(os.path.join(HN, "main.yml")) as f:
            self.assertIn("'ipv6': hn_ipv6_status", f.read())

    def test_the_teardown_wakes_after_the_deletion_is_complete(self):
        names = self.names(os.path.join(NPD, "main.yml"))
        self.assertLess(names.index("Mark deletion complete"), names.index("Tell the network that the placement is gone"))
        self.assertLess(names.index("Tell the network that the placement is gone"), names.index("Release the finalizer now"))
        # the wake has no condition of its own: every completed deletion (wake_network.yml skips a deleting network)
        t = by_name(load(os.path.join(NPD, "main.yml")), "Tell the network that the placement is gone")
        self.assertNotIn("when", t)
        self.assertEqual(t["ansible.builtin.include_tasks"], "wake_network.yml")
        # the short path for a placement refused at validation runs it too, after deletionComplete
        first = by_name(load(os.path.join(NPD, "main.yml")), "Skip the site cleanup when nothing was realized")
        self.assertEqual(first["vars"]["teardown_unrealized_after"], "{{ role_path }}/tasks/wake_network.yml")
        un = self.names(os.path.join(ROOT, "eda", "common", "tasks", "teardown_unrealized.yml"))
        self.assertLess(un.index("Record the deletion as complete"), un.index("The kind's steps after the deletion"))
        self.assertLess(un.index("The kind's steps after the deletion"), un.index("End the job"))

    def test_the_teardown_wakes_nothing_else_of_the_network_kind(self):
        # no double wake: the teardown's other wakes go to the gateway, waiting siblings, the fabric and the placement
        kinds = []
        for t in walk(load(os.path.join(NPD, "main.yml"))):
            crs = (t.get("vars") or {}).get("nudge_crs")
            if isinstance(crs, list):
                kinds += [c.get("kind") for c in crs]
            elif isinstance(crs, str) and "'kind': '" in crs:
                kinds += [crs.split("'kind': '")[1].split("'")[0]]
        self.assertNotIn("HybridNetwork", kinds)
        self.assertEqual(sorted(set(kinds)), ["CloudGateway", "HybridFabric", "NetworkPlacement"])

    def test_the_placement_wake_follows_the_ready_write(self):
        names = self.names(os.path.join(NP, "main.yml"))
        self.assertLess(names.index("Report the placement ready"),
                        names.index("Tell the network when the placement's provider-assigned subnet changed"))

    def test_the_network_never_wakes_a_ready_provider_placement(self):
        # no loop: the network's IPv6 row wake skips placements whose IPv6 subnet comes from the provider, and its other
        # placement wakes are for pending placements only
        t = by_name(load(os.path.join(HN, "main.yml")), "Wake the placements whose IPv6 internet row changed")
        self.assertIn("!= 'Provider'", t["vars"]["nudge_crs"])
        t = by_name(load(os.path.join(HN, "main.yml")), "Wake the placements waiting for address space")
        self.assertIn("== 'pending'", t["vars"]["nudge_crs"])

    def test_no_consumer_of_the_address_space_reads_the_list(self):
        readers = []
        for base, _dirs, files in os.walk(os.path.join(ROOT, "eda")):
            for f in files:
                if f.endswith((".yml", ".yaml", ".py", ".j2")):
                    with open(os.path.join(base, f)) as fh:
                        code = [ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("#")]
                    if any("providerPrefixes" in ln for ln in code):
                        readers.append(os.path.relpath(os.path.join(base, f), ROOT))
        # the writer (the network job) and the placement's wake, nothing else (comments don't count)
        self.assertEqual(sorted(readers), [
            "eda/rulebooks/roles/hybridnetwork_provision/tasks/main.yml",
            "eda/rulebooks/roles/hybridnetwork_provision/tasks/platform_block.yml",
            "eda/rulebooks/roles/networkplacement_provision/tasks/main.yml",
        ])

    def test_crd_schema(self):
        crd = load(CRD)
        st = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
        pp = st["ipv6"]["properties"]["providerPrefixes"]
        self.assertEqual(pp["type"], "array")
        self.assertEqual(sorted(pp["items"]["properties"]), ["placement", "prefix", "site"])
        for p in pp["items"]["properties"].values():
            self.assertEqual(p["type"], "string")
            self.assertTrue(p["description"])
        self.assertNotIn("providerPrefixes", st["addressSpace"]["items"]["properties"])
        self.assertEqual(sorted(st["addressSpace"]["items"]["properties"]),
                         ["allocations", "family", "largestFree", "prefix", "source"])


if __name__ == "__main__":
    unittest.main()
