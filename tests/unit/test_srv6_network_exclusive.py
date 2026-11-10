"""The SRv6 SID block and core.icmpSource6 are among the platform's exclusive ranges in the network job
(hybridnetwork_provision): a tenant prefix that overlaps either is refused (PrefixReserved)."""
import os
import unittest

import play_helper as ph

TASKS = os.path.join(ph.ROOT, "eda/rulebooks/roles/hybridnetwork_provision/tasks/main.yml")


@unittest.skipUnless(ph.HAVE_ANSIBLE, "ansible-playbook not installed")
class ExclusiveRanges(unittest.TestCase):
    def exclusive(self, fab_real):
        task = ph.by_name(ph.load(TASKS), "The platform's exclusive ranges")
        return ph.run_play([task, ph.write_out("hn_exclusive")], {"fab_real": fab_real})

    def test_without_a_block_the_list_is_as_before(self):
        real = {"network": {"exclusiveRanges": ["2001:db8:1::/48"]},
                "ipam": {"publicPools": {"a": {"prefixes": ["198.51.100.0/24"]}}}}
        want = ["2001:db8:1::/48", "198.51.100.0/24"]
        self.assertEqual(self.exclusive(real), want)
        self.assertEqual(self.exclusive(dict(real, core={"transport": "mpls", "srv6": {"block": ""}})), want)
        self.assertEqual(self.exclusive(dict(real, core={})), want)

    def test_the_block_is_exclusive_when_set(self):
        real = {"network": {"exclusiveRanges": ["2001:db8:1::/48"]}, "core": {"srv6": {"block": "5f00:db8::/32"}}}
        self.assertEqual(self.exclusive(real), ["2001:db8:1::/48", "5f00:db8::/32"])

    def test_the_icmp_source_pool_is_exclusive_when_set(self):
        """the gateways refuse every site route inside it (the nodes' own addresses, announced to the sites by
        default) and the cloud edge routers hold addresses from it, so no tenant prefix may overlap it, whether or not
        the installation lists it"""
        real = {"network": {"exclusiveRanges": ["2001:db8:1::/48"]},
                "core": {"icmpSource6": "2001:db8:4005:1::/64", "srv6": {"block": "5f00:db8::/32"}}}
        self.assertEqual(self.exclusive(real), ["2001:db8:1::/48", "5f00:db8::/32", "2001:db8:4005:1::/64"])
        self.assertEqual(self.exclusive(dict(real, core={"icmpSource6": ""})), ["2001:db8:1::/48"])
        # the realization defaults: none
        self.assertEqual(self.exclusive({"core": {"icmpSource6": ""}, "network": {"exclusiveRanges": []}}), [])


if __name__ == "__main__":
    unittest.main()
