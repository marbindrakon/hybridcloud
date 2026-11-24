"""Names on the underlying platforms come from one function (eda/common/files/shc_names.py).

What must hold: the person's own names in one order, joined by a character they cannot contain; exact while it fits
and every part obeys the rule; a derived ending when cut or changed, so two different inputs never give one name;
an access group never cut; the rule for what a person may type.
"""
import importlib.util
import itertools
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PATH = os.path.join(ROOT, "eda", "common", "files", "shc_names.py")
_spec = importlib.util.spec_from_file_location("shc_names", PATH)
names = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(names)

LABEL = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


class Rule(unittest.TestCase):
    def test_allowed(self):
        for v in ("a", "web", "acme-corp", "a1", "x" * 30, "core-wayne-atuin-v6"):
            self.assertEqual(names.check_name(v), "", v)

    def test_refused(self):
        for v in ("", "1web", "-web", "web-", "Web", "web.prod", "web_prod", "a--b", "x" * 31, "wéb", None):
            self.assertTrue(names.check_name(v), repr(v))

    def test_message_names_the_thing_and_the_limit(self):
        m = names.check_name("x" * 31, "cluster")
        self.assertIn("cluster", m)
        self.assertIn("30", m)
        self.assertIn("21", names.check_name("x" * 22, "gateway", names.GATEWAY_NAME_MAX))
        self.assertEqual(names.check_name("x" * 21, "gateway", names.GATEWAY_NAME_MAX), "")

    def test_a_very_long_value_is_not_echoed_whole(self):
        self.assertLess(len(names.check_name("x" * 5000)), 400)


class Exact(unittest.TestCase):
    def test_the_documented_examples(self):
        want = {
            ("openstack-project", ("acme-corp", "prod")): "acme-corp_prod",
            ("openstack-network", ("acme-corp", "core-prod")): "acme-corp_core-prod",
            ("openstack-cluster-user", ("acme-corp", "prod", "web")): "acme-corp_prod_web",
            ("site-namespace", ("acme-corp", "prod", "vms")): "acme-corp--prod--vms",
            ("gcp-placement", ("acme-corp", "core-prod")): "shc-acme-corp--core-prod",
            ("aws-placement", ("acme-corp", "core-prod")): "acme-corp_core-prod",
            ("registry-organization", ("acme-corp", "images")): "acme-corp_images",
            ("automation-organization", ("acme-corp", "automation")): "acme-corp_automation",
            ("identity-client", ("vault", "acme-corp", "web")): "vault_acme-corp_web",
        }
        for (kind, parts), name in want.items():
            got = names.name(kind, list(parts))
            self.assertEqual(got["name"], name, kind)
            self.assertTrue(got["exact"], kind)
            self.assertEqual(got["display"], " / ".join(parts))

    def test_every_pair_of_allowed_names_fits_keystone_exactly(self):
        got = names.name("openstack-project", ["a" * 30, "b" * 30])
        self.assertTrue(got["exact"])
        self.assertEqual(len(got["name"]), 61)
        self.assertEqual(names.group("a" * 30, "b" * 30)["name"], "a" * 30 + "." + "b" * 30)


class NoTwoInputsOneName(unittest.TestCase):
    PAIRS = [(("acme-corp", "web"), ("acme", "corp-web")),
             (("acme-corp", "web", "vms"), ("acme", "corp-web", "vms")),
             (("acme", "prod-vms", "a"), ("acme", "prod", "vms-a")),
             (("a-b", "c-d"), ("a", "b-c-d"))]

    def test_the_hyphen_pairs(self):
        for kind, spec in names.KINDS.items():
            for one, two in self.PAIRS:
                if spec[4] and spec[4] != len(one):
                    continue
                self.assertNotEqual(names.name(kind, list(one))["name"], names.name(kind, list(two))["name"],
                                    "%s %s %s" % (kind, one, two))

    def test_allowed_names_never_collide(self):
        pool = ["a", "b", "a-b", "b-a", "a-a", "ab", "a-b-a", "ba"]
        for kind in ("openstack-project", "site-namespace", "gcp-placement", "registry-organization"):
            seen = {}
            for parts in itertools.product(pool, repeat=names.KINDS[kind][4]):
                n = names.name(kind, list(parts))["name"]
                self.assertNotIn(n, seen, "%s: %s and %s" % (kind, parts, seen.get(n)))
                seen[n] = parts

    def test_long_names_differing_at_the_end_stay_apart(self):
        a = names.name("site-namespace", ["a" * 30, "b" * 30, "c" * 29 + "x"])
        b = names.name("site-namespace", ["a" * 30, "b" * 30, "c" * 29 + "y"])
        self.assertNotEqual(a["name"], b["name"])
        self.assertFalse(a["exact"])
        self.assertEqual(a["name"][:50], b["name"][:50])

    def test_a_changed_part_gets_the_derived_ending(self):
        dotted = names.name("site-namespace", ["acme", "web.prod", "vms"])
        hyphen = names.name("site-namespace", ["acme", "web-prod", "vms"])
        self.assertFalse(dotted["exact"])
        self.assertTrue(hyphen["exact"])
        self.assertNotEqual(dotted["name"], hyphen["name"])
        under = names.name("openstack-project", ["acme", "a_b"])
        self.assertFalse(under["exact"])
        self.assertNotEqual(under["name"], names.name("openstack-project", ["acme", "a-b"])["name"])
        self.assertNotEqual(under["name"], names.name("openstack-project", ["acme_a", "b"])["name"])


class Limits(unittest.TestCase):
    def test_cut_names_fit_with_their_endings_and_are_stable(self):
        for kind, (form, limit, prefix, reserve, count) in names.KINDS.items():
            parts = ["t" * 200, "e" * 200, "o" * 200][:count or 3]
            got = names.name(kind, parts)
            self.assertLessEqual(len(got["name"]) + reserve, limit, kind)
            self.assertFalse(got["exact"], kind)
            self.assertEqual(got, names.name(kind, list(parts)), kind)
            self.assertRegex(got["name"], r"[-_][0-9a-f]{%d}$" % names.DIGEST, kind)
            if form == "label":
                self.assertRegex(got["name"], LABEL, kind)
                self.assertNotIn("_", got["name"])

    def test_google_placement_names_fit_every_ending(self):
        for n in range(1, 31):
            got = names.name("gcp-placement", ["t" * 30, "p" * n])
            for ending in ("", "-fabric", "-fabric6", "-default4", "-default6", "-nat64", "-dns64", "-t0", "-iap"):
                self.assertLessEqual(len(got["name"] + ending), 63, (n, ending))
            self.assertRegex(got["name"], LABEL)

    def test_label_names_start_with_a_letter(self):
        got = names.name("site-namespace", ["1acme", "prod", "vms"])
        self.assertRegex(got["name"], LABEL)
        self.assertFalse(got["exact"])

    def test_a_missing_part_is_an_error(self):
        for parts in ([], ["acme"], ["acme", "prod", "web"], ["acme", ""], ["acme", None], ["", "prod"]):
            with self.assertRaises(ValueError):
                names.name("openstack-project", parts)
        with self.assertRaises(ValueError):
            names.name("no-such-kind", ["a", "b"])
        with self.assertRaises(ValueError):
            names.join(["a"], "other", 63)


class Group(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(names.group("acme-corp", "devs")["name"], "acme-corp.devs")

    def test_never_cut(self):
        with self.assertRaises(ValueError) as c:
            names.group("a" * 40, "b" * 40)
        self.assertIn("64", str(c.exception))
        self.assertEqual(len(names.group("a" * 40, "b" * 23)["name"]), 64)

    def test_a_tenant_with_a_dot_is_refused(self):
        with self.assertRaises(ValueError):
            names.group("acme.corp", "devs")


class Filter(unittest.TestCase):
    def call(self, req):
        p = subprocess.run([sys.executable, "-I", PATH], input=json.dumps(req), capture_output=True, text=True)
        return p.returncode, json.loads(p.stdout)

    def test_requests(self):
        self.assertEqual(self.call({"action": "name", "kind": "openstack-project", "parts": ["acme", "prod"]}),
                         (0, {"name": "acme_prod", "exact": True, "display": "acme / prod"}))
        self.assertEqual(self.call({"action": "check", "value": "web"}), (0, {"ok": True, "message": ""}))
        rc, out = self.call({"action": "check", "value": "Web", "what": "cluster"})
        self.assertEqual((rc, out["ok"]), (0, False))
        self.assertEqual(self.call({"action": "group", "tenant": "acme", "group": "devs"})[1]["name"], "acme.devs")
        self.assertEqual(self.call({"action": "join", "form": "label", "parts": ["a", "b"], "limit": 63})[1]["name"],
                         "a--b")

    def test_errors_are_json(self):
        for req in ({"action": "nothing"}, {"action": "name", "kind": "openstack-project", "parts": ["acme"]},
                    {"action": "group", "tenant": "a" * 40, "group": "b" * 40}):
            rc, out = self.call(req)
            self.assertEqual(rc, 1)
            self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
