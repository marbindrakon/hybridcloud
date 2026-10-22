"""Unit tests of the operator's Kafka publisher (operator/roles/_common/files/amq_publish.py); no broker needed."""
import os
import re
import sys
import types
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILES = os.path.join(ROOT, "operator", "roles", "_common", "files")
sys.path.insert(0, FILES)

try:
    from kafka import KafkaProducer  # noqa: F401

    HAVE_KAFKA = True
except ImportError:  # stub the client so the module imports
    HAVE_KAFKA = False
    stub = types.ModuleType("kafka")
    stub.KafkaProducer = type("KafkaProducer", (), {"DEFAULT_CONFIG": {}})
    sys.modules.setdefault("kafka", stub)

import amq_publish as A  # noqa: E402

# KafkaProducer.DEFAULT_CONFIG keys of kafka-python 2.0.x that 3.x dropped. 3.x raises
# "Unrecognized configs" for any of them.
REMOVED_IN_3 = {"api_version_auto_timeout_ms"}

ENV = {"KAFKA_BOOTSTRAP_SERVERS": "a:9093,b:9093", "KAFKA_USERNAME": "u", "KAFKA_PASSWORD": "p"}


def known_3x():
    """A stand-in for the 3.x DEFAULT_CONFIG: every key the module may pass except the removed ones."""
    keys = set(A.producer_kwargs(dict(ENV, KAFKA_API_VERSION="auto"), {A.PROBE_TIMEOUT_KEY: 0}))
    return {k: None for k in keys - REMOVED_IN_3}


class ProducerKwargsTest(unittest.TestCase):
    def test_pinned_version_by_default(self):
        kw = A.producer_kwargs(ENV, known_3x())
        self.assertEqual(kw["api_version"], (2, 6, 0))
        self.assertNotIn(A.PROBE_TIMEOUT_KEY, kw)
        self.assertEqual(kw["bootstrap_servers"], ["a:9093", "b:9093"])

    def test_auto_on_3x_passes_no_probe_timeout(self):
        kw = A.producer_kwargs(dict(ENV, KAFKA_API_VERSION="auto"), known_3x())
        self.assertIsNone(kw["api_version"])
        self.assertNotIn(A.PROBE_TIMEOUT_KEY, kw)

    def test_auto_on_2x_passes_probe_timeout(self):
        kw = A.producer_kwargs(dict(ENV, KAFKA_API_VERSION="auto"), {A.PROBE_TIMEOUT_KEY: 2000})
        self.assertEqual(kw[A.PROBE_TIMEOUT_KEY], A.API_VERSION_PROBE_MS)

    def test_bad_override_falls_back(self):
        self.assertEqual(A.producer_kwargs(dict(ENV, KAFKA_API_VERSION="x.y"), {})["api_version"], (2, 6, 0))
        self.assertEqual(A.producer_kwargs(dict(ENV, KAFKA_API_VERSION="3.7"), {})["api_version"], (3, 7))

    @unittest.skipUnless(HAVE_KAFKA, "kafka-python not installed")
    def test_kwargs_are_known_to_installed_client(self):
        for version in ("", "auto", "3.7.0"):
            kw = A.producer_kwargs(dict(ENV, KAFKA_API_VERSION=version), KafkaProducer.DEFAULT_CONFIG)
            unknown = set(kw) - set(KafkaProducer.DEFAULT_CONFIG)
            self.assertFalse(unknown, "KafkaProducer rejects {}".format(sorted(unknown)))

    @unittest.skipUnless(HAVE_KAFKA, "kafka-python not installed")
    def test_producer_builds_with_pinned_version(self):
        env = {"KAFKA_BOOTSTRAP_SERVERS": "127.0.0.1:1"}  # nothing listens; a pinned version does not connect
        producer = KafkaProducer(**A.producer_kwargs(env, KafkaProducer.DEFAULT_CONFIG))
        try:
            self.assertEqual(tuple(producer.config["api_version"])[:2], (2, 6))
        finally:
            producer.close(timeout=1)


class ImagePinTest(unittest.TestCase):
    def test_operator_images_pin_kafka_python(self):
        for image in ("primary", "namespace"):
            path = os.path.join(ROOT, "operator", image, "Dockerfile")
            with open(path) as f:
                text = f.read()
            self.assertRegex(text, r"kafka-python==\d+\.\d+\.\d+", path)
            self.assertFalse(re.search(r"kafka-python(\s|$)", text), path + " installs kafka-python unpinned")


class ApiVersionPassThroughTest(unittest.TestCase):
    """The protocol version is a chart value: the primary Deployment sets it, the tenant Deployment copies it."""

    def read(self, *parts):
        with open(os.path.join(ROOT, *parts)) as f:
            return f.read()

    def test_primary_deployment_sets_it_from_values(self):
        text = self.read("operator", "primary", "helm", "templates", "deployment.yaml")
        self.assertRegex(text, r"with \.Values\.amqKafka\.apiVersion\b[\s\S]*?name: KAFKA_API_VERSION")
        self.assertRegex(self.read("operator", "primary", "helm", "values.yaml"), r"(?m)^  apiVersion: \"\"$")

    def test_namespace_operator_deployment_copies_it(self):
        text = self.read("operator", "primary", "roles", "entity", "tasks", "deploy_namespace_operator.yml")
        self.assertRegex(text, r"name: KAFKA_API_VERSION\s+value: \"\{\{ lookup\('env', 'KAFKA_API_VERSION'\)")

    def test_publish_task_reads_it(self):
        text = self.read("operator", "roles", "_common", "tasks", "amq_publish.yml")
        self.assertIn("KAFKA_API_VERSION: \"{{ lookup('env', 'KAFKA_API_VERSION')", text)


if __name__ == "__main__":
    unittest.main()
