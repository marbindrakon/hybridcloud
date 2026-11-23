"""Every database the platform's charts create is backed up on a schedule and its old backups expire.

Without a schedule the backup repository keeps every write-ahead log since the first backup: it fills, the database
can no longer archive, its data volume fills too and it stops. Each PostgresCluster template therefore sets a number
of full backups to keep, a full and a differential schedule, and a repository for a full backup on demand. When the
repository cannot take write-ahead log, the segments that wait for it in the data volume are dropped past a limit
(archive-push-queue-max, by default a quarter of the data volume), so a broken repository costs the restore point and
not the database.

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

# chart -> the default limit of the waiting archive: a quarter of the chart's default data volume
QUEUE_MAX = {"aap-instance": "2560MiB", "aap": "2560MiB", "rhbk": "2560MiB", "gitea": "2560MiB", "quay": "5GiB"}


def storage_path(path):
    """The values path of the data volume's size, beside the backup settings."""
    return path.rsplit(".", 1)[0] + ".storage"


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


def charts_with_a_database():
    """Every chart with a template that creates a PostgresCluster, whatever the file is called: chart -> files."""
    found = {}
    for chart in sorted(os.listdir(CHARTS)):
        tdir = os.path.join(CHARTS, chart, "templates")
        for base, _dirs, files in os.walk(tdir):
            for name in files:
                with open(os.path.join(base, name), errors="replace") as f:
                    if "kind: PostgresCluster" in f.read():
                        found.setdefault(chart, []).append(os.path.relpath(os.path.join(base, name), tdir))
    return found


needs_helm = unittest.skipIf(HELM is None, "helm not installed")


@unittest.skipIf(yaml is None, "PyYAML not installed")
class BackupsExpire(unittest.TestCase):
    def test_every_database_template_is_covered(self):
        found = charts_with_a_database()
        self.assertEqual(sorted(found), sorted(DATABASES),
                         "a chart that creates a PostgresCluster is missing from this test, or one was removed")
        for chart, files in found.items():
            self.assertEqual(files, ["postgres.yaml"], "%s: this test renders templates/postgres.yaml only" % chart)

    @needs_helm
    def test_defaults_keep_two_full_backups_on_a_schedule(self):
        for chart, (on, _path, full) in DATABASES.items():
            with self.subTest(chart=chart):
                pgb = render(chart, on)["spec"]["backups"]["pgbackrest"]
                self.assertEqual(pgb["global"], {"repo1-retention-full": "2", "repo1-retention-full-type": "count",
                                                 "repo1-retention-diff": "2",
                                                 "archive-push-queue-max": QUEUE_MAX[chart]})
                repos = pgb["repos"]
                self.assertEqual([r["name"] for r in repos], ["repo1"])
                self.assertEqual(repos[0]["schedules"]["full"], full)
                self.assertEqual(repos[0]["schedules"]["differential"], full.replace("* * 0", "* * 1-6"))
                self.assertIn("volume", repos[0])
                self.assertEqual(pgb["manual"], {"repoName": "repo1", "options": ["--type=full"]})

    def test_the_schedules_do_not_start_at_the_same_minute(self):
        minutes = [full.split()[0] for _on, _path, full in DATABASES.values()]
        self.assertEqual(len(minutes), len(set(minutes)), "two databases would start their backups together")

    @needs_helm
    def test_the_settings_follow_the_values(self):
        for chart, (on, path, _full) in DATABASES.items():
            with self.subTest(chart=chart):
                sets = on + [path + ".retentionFull=5", path + ".retentionDifferential=3",
                             path + ".fullSchedule=7 3 * * 6", path + ".differentialSchedule=7 3 * * 0-5"]
                pgb = render(chart, sets)["spec"]["backups"]["pgbackrest"]
                self.assertEqual(pgb["global"]["repo1-retention-full"], "5")
                self.assertEqual(pgb["global"]["repo1-retention-diff"], "3")
                self.assertEqual(pgb["repos"][0]["schedules"],
                                 {"full": "7 3 * * 6", "differential": "7 3 * * 0-5"})

    @needs_helm
    def test_an_empty_differential_schedule_means_no_differential_backups(self):
        for chart, (on, path, full) in DATABASES.items():
            with self.subTest(chart=chart):
                pgb = render(chart, on + [path + ".differentialSchedule="])["spec"]["backups"]["pgbackrest"]
                self.assertEqual(pgb["repos"][0]["schedules"], {"full": full})

    @needs_helm
    def test_values_without_the_backup_settings_get_the_defaults(self):
        for chart, (on, path, full) in DATABASES.items():
            with self.subTest(chart=chart):
                pgb = render(chart, on + [path + "=null"])["spec"]["backups"]["pgbackrest"]
                self.assertEqual(pgb["global"]["repo1-retention-full"], "2")
                self.assertEqual(pgb["global"]["archive-push-queue-max"], QUEUE_MAX[chart])
                self.assertEqual(pgb["repos"][0]["schedules"],
                                 {"full": full, "differential": full.replace("* * 0", "* * 1-6")})

    def test_the_values_files_document_the_defaults_the_templates_use(self):
        for chart, (_on, path, full) in DATABASES.items():
            with self.subTest(chart=chart):
                with open(os.path.join(CHARTS, chart, "values.yaml")) as f:
                    node = yaml.safe_load(f)
                for key in path.split("."):
                    node = node[key]
                self.assertEqual(node, {"retentionFull": 2, "retentionDifferential": 2, "fullSchedule": full,
                                        "differentialSchedule": full.replace("* * 0", "* * 1-6"),
                                        "archivePushQueueMax": "auto"})


@unittest.skipIf(yaml is None, "PyYAML not installed")
class TheWaitingArchiveIsLimited(unittest.TestCase):
    """archive-push-queue-max: a quarter of the data volume by default, a size as given, empty for no limit; never
    silently no limit when the quarter cannot be computed."""

    def queue_max(self, chart, sets):
        return render(chart, DATABASES[chart][0] + sets)["spec"]["backups"]["pgbackrest"]["global"].get(
            "archive-push-queue-max")

    @needs_helm
    def test_the_default_is_a_quarter_of_the_data_volume(self):
        for chart, (_on, path, _full) in DATABASES.items():
            with self.subTest(chart=chart):
                st = storage_path(path)
                self.assertEqual(self.queue_max(chart, [st + "=80Gi"]), "20GiB")
                self.assertEqual(self.queue_max(chart, [st + "=10Gi"]), "2560MiB")
                self.assertEqual(self.queue_max(chart, [st + "=1Ti"]), "256GiB")
                self.assertEqual(self.queue_max(chart, [st + "=4000Mi"]), "1000MiB")
                self.assertEqual(self.queue_max(chart, [st + "=10Gi", path + ".archivePushQueueMax=auto"]),
                                 "2560MiB")

    @needs_helm
    def test_a_size_overrides_and_empty_means_no_limit(self):
        for chart, (_on, path, _full) in DATABASES.items():
            with self.subTest(chart=chart):
                self.assertEqual(self.queue_max(chart, [path + ".archivePushQueueMax=4GiB"]), "4GiB")
                self.assertIsNone(self.queue_max(chart, [path + ".archivePushQueueMax="]))
                # a size given explicitly needs no computable data volume
                self.assertEqual(self.queue_max(chart, [storage_path(path) + "=100G",
                                                        path + ".archivePushQueueMax=25GB"]), "25GB")

    @needs_helm
    def test_a_limit_that_cannot_be_computed_or_read_is_refused_with_the_settings_name(self):
        for chart, (on, path, _full) in DATABASES.items():
            for sets in ([storage_path(path) + "=100G"], [storage_path(path) + "=2Mi"],
                         [path + ".archivePushQueueMax=2.5GiB"], [path + ".archivePushQueueMax=a lot"]):
                with self.subTest(chart=chart, sets=sets):
                    with self.assertRaises(AssertionError) as err:
                        render(chart, on + sets)
                    self.assertIn(path + ".archivePushQueueMax", str(err.exception))


if __name__ == "__main__":
    unittest.main()
