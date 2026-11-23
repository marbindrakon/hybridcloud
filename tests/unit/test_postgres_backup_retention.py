"""Every database the platform's charts create is backed up on a schedule and its old backups expire.

Without a schedule the backup repository keeps every write-ahead log since the first backup: it fills, the database
can no longer archive, its data volume fills too and it stops. Each PostgresCluster template therefore sets a number
of full backups to keep, a full and a differential schedule, and a repository for a full backup on demand.

Each chart's templates/postgres.yaml is rendered alone (a scratch chart with the chart's values and helpers), so a
chart with subchart dependencies renders without fetching them.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHARTS = os.path.join(ROOT, "bootstrap", "helm", "charts")
HELM = shutil.which("helm")

# chart -> (values that switch the database on, the values path of the backup settings, default full schedule)
DATABASES = {
    "aap-instance": (["postgres.enabled=true", "namespace=x"], "postgres.backup", "0 1 * * 0"),
    "aap": (["postgres.enabled=true"], "postgres.backup", "10 1 * * 0"),
    "rhbk": (["keycloak.enabled=true", "keycloak.db.enabled=true", "keycloak.db.crunchy.enabled=true"],
             "keycloak.db.crunchy.backup", "20 1 * * 0"),
    "gitea": (["crunchy.enabled=true"], "crunchy.backup", "30 1 * * 0"),
    "quay": (["crunchy.enabled=true"], "crunchy.backup", "40 1 * * 0"),
}


def render(chart, sets):
    """The PostgresCluster of a chart, rendered from its postgres template alone."""
    src = os.path.join(CHARTS, chart)
    with tempfile.TemporaryDirectory(prefix="shc-pgbackup-") as tmp:
        os.makedirs(os.path.join(tmp, "templates"))
        with open(os.path.join(tmp, "Chart.yaml"), "w") as f:
            f.write("apiVersion: v2\nname: %s\nversion: 0.0.0\n" % chart)
        shutil.copy(os.path.join(src, "values.yaml"), tmp)
        shutil.copy(os.path.join(src, "templates", "postgres.yaml"), os.path.join(tmp, "templates"))
        helpers = os.path.join(src, "templates", "_helpers.tpl")
        if os.path.exists(helpers):
            shutil.copy(helpers, os.path.join(tmp, "templates"))
        args = [HELM, "template", "t", tmp]
        for s in sets:
            args += ["--set", s]
        out = subprocess.run(args, capture_output=True, text=True)
        if out.returncode != 0:
            raise AssertionError("helm template %s failed: %s" % (chart, out.stderr.strip()[:400]))
    docs = [d for d in yaml.safe_load_all(out.stdout) if d and d.get("kind") == "PostgresCluster"]
    if len(docs) != 1:
        raise AssertionError("%s rendered %d PostgresCluster objects" % (chart, len(docs)))
    return docs[0]


@unittest.skipIf(yaml is None, "PyYAML not installed")
@unittest.skipIf(HELM is None, "helm not installed")
class BackupsExpire(unittest.TestCase):
    def test_every_database_template_is_covered(self):
        found = sorted(c for c in os.listdir(CHARTS)
                       if os.path.exists(os.path.join(CHARTS, c, "templates", "postgres.yaml")))
        self.assertEqual(found, sorted(DATABASES),
                         "a chart with a database template is missing from this test, or one was removed")

    def test_defaults_keep_two_full_backups_on_a_schedule(self):
        for chart, (on, _path, full) in DATABASES.items():
            with self.subTest(chart=chart):
                pgb = render(chart, on)["spec"]["backups"]["pgbackrest"]
                self.assertEqual(pgb["global"], {"repo1-retention-full": "2", "repo1-retention-full-type": "count"})
                repos = pgb["repos"]
                self.assertEqual([r["name"] for r in repos], ["repo1"])
                self.assertEqual(repos[0]["schedules"]["full"], full)
                self.assertEqual(repos[0]["schedules"]["differential"], full.replace("* * 0", "* * 1-6"))
                self.assertIn("volume", repos[0])
                self.assertEqual(pgb["manual"], {"repoName": "repo1", "options": ["--type=full"]})

    def test_the_schedules_do_not_start_at_the_same_minute(self):
        minutes = [full.split()[0] for _on, _path, full in DATABASES.values()]
        self.assertEqual(len(minutes), len(set(minutes)), "two databases would start their backups together")

    def test_the_settings_follow_the_values(self):
        for chart, (on, path, _full) in DATABASES.items():
            with self.subTest(chart=chart):
                sets = on + [path + ".retentionFull=5", path + ".fullSchedule=7 3 * * 6",
                             path + ".differentialSchedule=7 3 * * 0-5"]
                pgb = render(chart, sets)["spec"]["backups"]["pgbackrest"]
                self.assertEqual(pgb["global"]["repo1-retention-full"], "5")
                self.assertEqual(pgb["repos"][0]["schedules"],
                                 {"full": "7 3 * * 6", "differential": "7 3 * * 0-5"})

    def test_the_values_files_document_the_defaults_the_templates_use(self):
        for chart, (_on, path, full) in DATABASES.items():
            with self.subTest(chart=chart):
                with open(os.path.join(CHARTS, chart, "values.yaml")) as f:
                    node = yaml.safe_load(f)
                for key in path.split("."):
                    node = node[key]
                self.assertEqual(node, {"retentionFull": 2, "fullSchedule": full,
                                        "differentialSchedule": full.replace("* * 0", "* * 1-6")})


if __name__ == "__main__":
    unittest.main()
