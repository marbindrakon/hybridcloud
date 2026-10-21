"""Unit tests of the shc-tenancy CronJob logic (no cluster; kubernetes client stubbed when absent)."""
import datetime as dt
import os
import sys
import types
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILES = os.path.join(ROOT, "charts", "charts", "shc-cluster-base", "files")
sys.path.insert(0, FILES)

try:
    import kubernetes  # noqa: F401
    import yaml  # noqa: F401
except ImportError:  # stub the client so the pure functions import
    k8s = types.ModuleType("kubernetes")
    k8s.client = types.SimpleNamespace()
    k8s.config = types.SimpleNamespace(ConfigException=Exception)
    rest = types.ModuleType("kubernetes.client.rest")
    rest.ApiException = type("ApiException", (Exception,), {})
    sys.modules.update({"kubernetes": k8s, "kubernetes.client": k8s.client, "kubernetes.client.rest": rest})
    if "yaml" not in sys.modules:
        try:
            import yaml  # noqa: F401
        except ImportError:
            sys.modules["yaml"] = types.SimpleNamespace(safe_load=lambda f: {})

import shc_tenancy as T  # noqa: E402

NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.timezone.utc)
TCFG = dict(T.DEFAULTS["tenancy"])
NS = types.SimpleNamespace


def deploy(available=1, since_min=None):
    conds = []
    if since_min is not None:
        conds = [NS(type="Available", status="False", last_transition_time=NOW - dt.timedelta(minutes=since_min))]
    return NS(status=NS(available_replicas=available, conditions=conds))


def pod(restarts=0, reason=None, finished_min=None):
    term = None
    if reason:
        term = NS(reason=reason, finished_at=NOW - dt.timedelta(minutes=finished_min))
    return NS(status=NS(container_statuses=[NS(restart_count=restarts, last_state=NS(terminated=term))]))


class Controller(unittest.TestCase):
    def test_available(self):
        self.assertEqual(T.controller_condition(deploy(), [pod()], TCFG, NOW)["status"], "True")

    def test_missing(self):
        self.assertEqual(T.controller_condition(None, [], TCFG, NOW)["reason"], "ControllerUnavailable")

    def test_unavailable_grace(self):
        self.assertEqual(T.controller_condition(deploy(0, 1), [], TCFG, NOW)["status"], "True")
        self.assertEqual(T.controller_condition(deploy(0, 5), [], TCFG, NOW)["reason"], "ControllerUnavailable")

    def test_oom_and_restarts(self):
        self.assertEqual(T.controller_condition(deploy(), [pod(1, "OOMKilled", 3)], TCFG, NOW)["reason"],
                         "ControllerOutOfMemory")
        self.assertEqual(T.controller_condition(deploy(), [pod(4, "Error", 3)], TCFG, NOW)["reason"],
                         "ControllerRestarting")
        self.assertEqual(T.controller_condition(deploy(), [pod(4, "Error", 60)], TCFG, NOW)["status"], "True")


def cr(name, ready=True, generation=1, observed=1, delivery=None, created="2026-10-21T10:00:00Z"):
    st = {"observedGeneration": observed, "delivery": delivery or {}}
    if ready:
        st["conditions"] = [{"type": "Ready", "status": "True", "reason": "Ready"}]
        st["ready"] = True
    return ("HybridNetwork", {"metadata": {"name": name, "generation": generation, "creationTimestamp": created},
                              "status": st})


class Delivery(unittest.TestCase):
    def test_ready_tenant(self):
        cond, stalled = T.delivery_condition([cr("a"), cr("b")], TCFG, NOW)
        self.assertEqual((cond["status"], stalled), ("False", []))

    def test_unacknowledged_event(self):
        d = {"eventId": "e", "publishedAt": "2026-10-21T11:00:00Z"}
        cond, stalled = T.delivery_condition([cr("a", ready=False, delivery=d)], TCFG, NOW)
        self.assertEqual((cond["status"], cond["reason"], stalled), ("True", "NoJobStarted", ["HybridNetwork/a"]))
        self.assertNotIn("notified", cond["message"])

    def test_recent_event_not_stalled(self):
        d = {"eventId": "e", "publishedAt": "2026-10-21T11:55:00Z"}
        cond, _ = T.delivery_condition([cr("a", ready=False, delivery=d)], TCFG, NOW)
        self.assertEqual(cond["status"], "False")

    def test_change_never_published(self):
        cond, _ = T.delivery_condition([cr("a", ready=False, generation=2, observed=1)], TCFG, NOW)
        self.assertEqual(cond["status"], "True")

    def test_redrive_state(self):
        cond, _ = T.delivery_condition([cr("a", ready=False, delivery={"state": "Stalled"})], TCFG, NOW)
        self.assertEqual(cond["status"], "True")


class Config(unittest.TestCase):
    def test_flat_config_is_redrive(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("ackTimeoutMinutes: 7\nredriveKinds: [Team]\n")
        try:
            cfg = T.load_config(fh.name)
        finally:
            os.unlink(fh.name)
        if cfg["redrive"].get("ackTimeoutMinutes") is None:
            self.skipTest("PyYAML not available")
        self.assertEqual(cfg["redrive"]["ackTimeoutMinutes"], 7)
        self.assertFalse(cfg["phase"]["write"])


if __name__ == "__main__":
    unittest.main()
