"""The Keycloak's database connections after a fail-over (bootstrap/helm/charts/rhbk: keycloak.db.urlProperties,
keycloak.db.poolMaxLifetime, keycloak.db.crunchy.patroniFailsafe) and the central chart's two Keycloak Applications,
rendered with helm template.

With the chart's own database the JDBC driver accepts only the primary (db-url-properties ?targetServerType=primary),
and every connection is replaced after at most 10 minutes (db-pool-max-lifetime). Both are server options in
spec.additionalOptions, so neither Application may ignore that field: with RespectIgnoreDifferences an ignored field
is never applied. Patroni's fail-safe mode is off unless switched on. Names are examples."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT  # noqa: E402

CHART = os.path.join(ROOT, "bootstrap", "helm", "charts", "rhbk")
CENTRAL = os.path.join(ROOT, "bootstrap", "helm", "central")
BASE = {"namespace": "kc-ns", "instanceName": "kc"}
OWN_DB = {"enabled": True, "host": "db-primary.kc-ns.svc", "crunchy": {"enabled": True}}


def render(db=None):
    values = dict(BASE)
    if db is not None:
        values["keycloak"] = {"db": db}
    return render_values(values)


def render_values(values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
    try:
        p = subprocess.run(["helm", "template", "kc", CHART, "-f", f.name], capture_output=True, text=True,
                           timeout=120)
    finally:
        os.unlink(f.name)
    docs = [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)] if p.returncode == 0 else []
    return p, docs


def options(docs):
    cr = next(d for d in docs if d.get("kind") == "Keycloak")
    return {o["name"]: o["value"] for o in cr["spec"]["additionalOptions"]}


def cluster(docs):
    return next(d for d in docs if d.get("kind") == "PostgresCluster")


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class Connections(unittest.TestCase):
    def ok(self, db=None):
        p, docs = render(db)
        self.assertEqual(p.returncode, 0, p.stderr)
        return docs

    def test_own_database_accepts_only_the_primary_and_connections_live_ten_minutes(self):
        opts = options(self.ok(OWN_DB))
        self.assertEqual(opts["db-url-properties"], "?targetServerType=primary")
        self.assertEqual(opts["db-pool-max-lifetime"], "10m")
        # the options of before stay as they were
        self.assertEqual(opts["http-enabled"], "true")
        self.assertEqual(opts["hostname-strict"], "false")

    def test_another_database_gets_no_url_properties_by_default(self):
        opts = options(self.ok({"enabled": True, "host": "pg.example.org"}))
        self.assertNotIn("db-url-properties", opts)
        self.assertEqual(opts["db-pool-max-lifetime"], "10m")

    def test_without_a_database_nothing_is_added(self):
        opts = options(self.ok())
        self.assertEqual(opts, {"http-enabled": "true", "hostname-strict": "false"})

    def test_values_override_and_switch_off(self):
        opts = options(self.ok(dict(OWN_DB, urlProperties="?targetServerType=primary&connectTimeout=5",
                                    poolMaxLifetime="PT5M")))
        self.assertEqual(opts["db-url-properties"], "?targetServerType=primary&connectTimeout=5")
        self.assertEqual(opts["db-pool-max-lifetime"], "PT5M")
        opts = options(self.ok(dict(OWN_DB, poolMaxLifetime=600)))
        self.assertEqual(opts["db-pool-max-lifetime"], "600")
        opts = options(self.ok(dict(OWN_DB, urlProperties="", poolMaxLifetime="")))
        self.assertNotIn("db-url-properties", opts)
        self.assertNotIn("db-pool-max-lifetime", opts)
        opts = options(self.ok({"enabled": True, "host": "pg.example.org", "urlProperties": "?sslmode=require"}))
        self.assertEqual(opts["db-url-properties"], "?sslmode=require")

    def test_auto_needs_postgres_too(self):
        opts = options(self.ok(dict(OWN_DB, vendor="mysql")))
        self.assertNotIn("db-url-properties", opts)

    def test_null_values_get_the_template_defaults(self):
        opts = options(self.ok(dict(OWN_DB, urlProperties=None, poolMaxLifetime=None)))
        self.assertEqual(opts["db-url-properties"], "?targetServerType=primary")
        self.assertEqual(opts["db-pool-max-lifetime"], "10m")

    def test_iso_durations(self):
        for good in ("PT10M", "P1DT2H", "PT1.5S", "600", "10m", "500ms"):
            with self.subTest(value=good):
                self.assertEqual(options(self.ok(dict(OWN_DB, poolMaxLifetime=good)))["db-pool-max-lifetime"], good)

    def test_wrong_values_are_refused_with_the_settings_name(self):
        for db, text in ((dict(OWN_DB, urlProperties="targetServerType=primary"), "keycloak.db.urlProperties"),
                         (dict(OWN_DB, poolMaxLifetime="ten minutes"), "keycloak.db.poolMaxLifetime"),
                         (dict(OWN_DB, poolMaxLifetime="P"), "keycloak.db.poolMaxLifetime"),
                         (dict(OWN_DB, poolMaxLifetime="PT"), "keycloak.db.poolMaxLifetime"),
                         (dict(OWN_DB, poolMaxLifetime="PXYZ"), "keycloak.db.poolMaxLifetime"),
                         (dict(OWN_DB, poolMaxLifetime="10x"), "keycloak.db.poolMaxLifetime")):
            with self.subTest(db=db):
                p, _ = render(db)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(text, p.stderr)

    def test_further_options_follow_the_charts_own(self):
        values = dict(BASE, keycloak={"db": OWN_DB, "additionalOptions": [
            {"name": "log-level", "value": "INFO"},
            {"name": "example-option", "secret": {"name": "example-secret", "key": "example-key"}},
            {"name": "example-number", "value": 3}]})
        p, docs = render_values(values)
        self.assertEqual(p.returncode, 0, p.stderr)
        cr = next(d for d in docs if d.get("kind") == "Keycloak")
        self.assertEqual([o["name"] for o in cr["spec"]["additionalOptions"]],
                         ["http-enabled", "hostname-strict", "db-url-properties", "db-pool-max-lifetime",
                          "log-level", "example-option", "example-number"])
        self.assertEqual(cr["spec"]["additionalOptions"][5],
                         {"name": "example-option", "secret": {"name": "example-secret", "key": "example-key"}})
        self.assertEqual(cr["spec"]["additionalOptions"][6], {"name": "example-number", "value": "3"})

    def test_further_options_cannot_set_the_charts_own(self):
        for name in ("http-enabled", "hostname-strict", "db-url-properties", "db-pool-max-lifetime", "db-url",
                     "db-url-host", "db-url-database", "db-url-port", "db", "db-username", "db-password"):
            with self.subTest(option=name):
                p, _ = render_values(dict(BASE, keycloak={"db": OWN_DB,
                                                          "additionalOptions": [{"name": name, "value": "x"}]}))
                self.assertNotEqual(p.returncode, 0)
                self.assertIn("keycloak.additionalOptions: %s is set by the chart" % name, p.stderr)
        for bad in ([{"name": "x"}], [{"value": "1"}], {"x": "1"}):
            with self.subTest(entry=bad):
                p, _ = render_values(dict(BASE, keycloak={"db": OWN_DB, "additionalOptions": bad}))
                self.assertNotEqual(p.returncode, 0)
                self.assertIn("keycloak.additionalOptions", p.stderr)

    def test_further_options_render_whole_numbers_and_switches_as_text(self):
        values = dict(BASE, keycloak={"db": OWN_DB, "additionalOptions": [
            {"name": "example-large", "value": 10000000}, {"name": "example-switch", "value": True},
            {"name": "example-limit", "value": 2 ** 53}, {"name": "example-pool", "value": "db-pool-min-size"},
            {"name": "db-pool-min-size", "value": 2},
            {"name": "example-text", "value": "2.5"}]})
        p, docs = render_values(values)
        self.assertEqual(p.returncode, 0, p.stderr)
        got = options(docs)
        self.assertEqual((got["example-large"], got["example-switch"], got["example-text"], got["example-limit"],
                          got["db-pool-min-size"]), ("10000000", "true", "2.5", str(2 ** 53), "2"))

    def test_a_malformed_further_option_is_refused_with_its_name(self):
        name = "example-option"
        cases = [
            ([{"name": name, "value": None}], "the value must be a text, a whole number or true or false, not null"),
            ([{"name": name, "value": {"a": 1}}], "the value must be a text, a whole number or true or false, not map"),
            ([{"name": name, "value": ["a"]}], "the value must be a text, a whole number or true or false, not slice"),
            ([{"name": name, "value": 2.5}], "a number must be whole"),
            ([{"name": name, "value": 2 ** 60}], "a number above 2^53 is not exact"),
            ([{"name": name, "value": -(2 ** 60)}], "a number above 2^53 is not exact"),
            ([{"name": name, "value": "x", "secret": {"name": "s", "key": "k"}}], "a value or a secret, not both"),
            ([{"name": name, "secret": "s"}], "secret must be {name, key}"),
            ([{"name": name, "secret": {"name": "s"}}], "secret must be {name, key}"),
            ([{"name": name, "secret": {"key": "k"}}], "secret must be {name, key}"),
            ([{"name": name, "secret": {"name": "", "key": "k"}}], "secret must be {name, key}"),
            ([{"name": name, "secret": {"name": "s", "key": "k", "other": "x"}}], "secret must be {name, key}"),
            ([{"name": name, "secret": None}], "secret must be {name, key}"),
            ([{"name": name}], "needs a value or a secret"),
            ([{"name": name, "value": "a"}, {"name": name, "value": "b"}], "the option is listed twice"),
        ]
        for bad, text in cases:
            with self.subTest(entry=bad):
                p, _ = render_values(dict(BASE, keycloak={"db": OWN_DB, "additionalOptions": bad}))
                self.assertNotEqual(p.returncode, 0)
                self.assertIn("keycloak.additionalOptions[%s]: %s" % (name, text), p.stderr)
        for bad in ([{"name": ""}], [{"name": 3, "value": "x"}], ["log-level"]):
            with self.subTest(entry=bad):
                p, _ = render_values(dict(BASE, keycloak={"db": OWN_DB, "additionalOptions": bad}))
                self.assertIn("keycloak.additionalOptions: each entry needs a name", p.stderr)

    def test_failsafe_is_off_unless_switched_on(self):
        self.assertNotIn("patroni", cluster(self.ok(OWN_DB))["spec"])
        spec = cluster(self.ok(dict(OWN_DB, crunchy={"enabled": True, "patroniFailsafe": True})))["spec"]
        self.assertEqual(spec["patroni"], {"dynamicConfiguration": {"failsafe_mode": True}})

    def test_the_values_file_documents_the_defaults(self):
        with open(os.path.join(CHART, "values.yaml")) as f:
            db = yaml.safe_load(f)["keycloak"]["db"]
        self.assertEqual(db["urlProperties"], "auto")
        self.assertEqual(db["poolMaxLifetime"], "10m")
        self.assertIs(db["crunchy"]["patroniFailsafe"], False)
        with open(os.path.join(CHART, "values.yaml")) as f:
            self.assertEqual(yaml.safe_load(f)["keycloak"]["additionalOptions"], [])


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class Applications(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = subprocess.run(["helm", "template", "central", CENTRAL], capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise AssertionError(p.stderr[-2000:])
        cls.docs = [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)]

    def ignored(self, name):
        app = next(d for d in self.docs if d.get("kind") == "Application" and d["metadata"]["name"] == name)
        return [p for d in app["spec"].get("ignoreDifferences") or [] if d.get("kind") == "Keycloak"
                for p in d.get("jsonPointers", [])]

    def test_neither_application_ignores_the_server_options(self):
        for name in ("rhbk-central", "rhbk-services"):
            with self.subTest(application=name):
                self.assertNotIn("/spec/additionalOptions", self.ignored(name))

    def test_what_else_stays_as_it_was(self):
        self.assertEqual(self.ignored("rhbk-central"), ["/spec/hostname"])
        self.assertEqual(self.ignored("rhbk-services"), [])


if __name__ == "__main__":
    unittest.main()
