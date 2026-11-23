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

    def test_wrong_values_are_refused_with_the_settings_name(self):
        for db, text in ((dict(OWN_DB, urlProperties="targetServerType=primary"), "keycloak.db.urlProperties"),
                         (dict(OWN_DB, poolMaxLifetime="ten minutes"), "keycloak.db.poolMaxLifetime"),
                         (dict(OWN_DB, poolMaxLifetime="10x"), "keycloak.db.poolMaxLifetime")):
            with self.subTest(db=db):
                p, _ = render(db)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(text, p.stderr)

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
