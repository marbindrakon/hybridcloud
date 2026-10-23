#!/usr/bin/env python3
"""
Event forwarder: watches Kubernetes events on the services cluster and
publishes matching operator events to AMQ Streams Kafka and (optionally)
the legacy AAP EDA Event Stream on central.
"""

import fnmatch
import json
import logging
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException

CLUSTER = "services"
EVENT_STREAM_URL = os.environ.get("EVENT_STREAM_URL", "")
EVENT_STREAM_TOKEN = os.environ.get("EVENT_STREAM_TOKEN", "")
KAFKA_ENABLED = os.environ.get("KAFKA_ENABLED", "true").lower() == "true"
KAFKA_BOOTSTRAP_SERVERS = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    "hybridsovereign-kafka-kafka-bootstrap.amq-streams.svc:9092",
)
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "hybridsovereign-events")
# Kafka protocol version the producer speaks. Pinned so the producer does not probe the broker version first: the
# probe has a short timeout (2 s by default), and one slow DNS resolver made it fail although the next resolver
# answered. KAFKA_API_VERSION overrides it ("auto" probes, with API_VERSION_PROBE_MS). Same as amq_publish.py.
DEFAULT_KAFKA_API_VERSION = "2.6.0"
API_VERSION_PROBE_MS = 10000
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))
DEDUP_CACHE_SIZE = 10_000
MAX_RETRIES = 3
BACKOFF_SECONDS = (1, 2, 4)
WATCH_TIMEOUT = 300
RECONNECT_BACKOFF = 5

NAMESPACE_PATTERNS = ("sovereign-cloud", "sovereign-cloud-plugins", "entity-*")
REPORTING_CONTROLLER_RE = re.compile(r".+-operator$")
REASON_RE = re.compile(r".+Requested$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_kafka_producer = None


def kafka_api_version(value):
    value = (value or "").strip()
    if value.lower() == "auto":
        return None
    try:
        return tuple(int(p) for p in (value or DEFAULT_KAFKA_API_VERSION).split("."))
    except ValueError:
        return tuple(int(p) for p in DEFAULT_KAFKA_API_VERSION.split("."))


def get_kafka_producer():
    global _kafka_producer
    if not KAFKA_ENABLED:
        return None
    if _kafka_producer is not None:
        return _kafka_producer
    try:
        from kafka import KafkaProducer  # type: ignore

        kwargs = {
            "bootstrap_servers": KAFKA_BOOTSTRAP_SERVERS.split(","),
            "value_serializer": lambda v: json.dumps(v).encode("utf-8"),
            "key_serializer": lambda k: k.encode("utf-8") if k else None,
            "acks": "all",
            "retries": 3,
            "api_version": kafka_api_version(os.environ.get("KAFKA_API_VERSION", "")),
        }
        # kafka-python 3.x rejects unknown configs and has no probe timeout option.
        if kwargs["api_version"] is None and "api_version_auto_timeout_ms" in KafkaProducer.DEFAULT_CONFIG:
            kwargs["api_version_auto_timeout_ms"] = API_VERSION_PROBE_MS
        _kafka_producer = KafkaProducer(**kwargs)
        logger.info("Kafka producer connected to %s topic=%s", KAFKA_BOOTSTRAP_SERVERS, KAFKA_TOPIC)
        return _kafka_producer
    except Exception as exc:
        logger.warning("Kafka producer unavailable: %s", exc)
        return None


class LRUCache:
    def __init__(self, maxsize: int):
        self.maxsize = maxsize
        self._entries: OrderedDict[str, None] = OrderedDict()

    def add(self, key: str) -> bool:
        if key in self._entries:
            self._entries.move_to_end(key)
            return False
        self._entries[key] = None
        if len(self._entries) > self.maxsize:
            self._entries.popitem(last=False)
        return True


def namespace_matches(namespace: str) -> bool:
    return any(fnmatch.fnmatch(namespace, pattern) for pattern in NAMESPACE_PATTERNS)


def event_matches_dict(event_dict: dict) -> bool:
    metadata = event_dict.get("metadata", {})
    namespace = metadata.get("namespace", "")
    if not namespace or not namespace_matches(namespace):
        return False
    reporting = (
        event_dict.get("reportingController", "")
        or event_dict.get("reportingComponent", "")
        or event_dict.get("source", {}).get("component", "")
    )
    if not REPORTING_CONTROLLER_RE.match(reporting):
        return False
    return bool(REASON_RE.match(event_dict.get("reason", "")))


def format_timestamp(ts_str) -> str:
    if not ts_str:
        return ""
    if isinstance(ts_str, str):
        return ts_str
    try:
        return ts_str.isoformat().replace("+00:00", "Z")
    except Exception:
        return str(ts_str)


def normalize_event_dict(event_dict: dict) -> dict:
    metadata = event_dict.get("metadata", {})
    regarding = event_dict.get("regarding", {}) or event_dict.get("involvedObject", {})
    ts = (
        event_dict.get("eventTime")
        or event_dict.get("lastTimestamp")
        or event_dict.get("firstTimestamp")
        or ""
    )
    reporting = (
        event_dict.get("reportingController", "")
        or event_dict.get("reportingComponent", "")
        or event_dict.get("source", {}).get("component", "")
    )
    return {
        "event_uid": metadata.get("uid", ""),
        "reason": event_dict.get("reason", ""),
        "action": event_dict.get("action", ""),
        "regarding": {
            "apiVersion": regarding.get("apiVersion", ""),
            "kind": regarding.get("kind", ""),
            "name": regarding.get("name", ""),
            "namespace": regarding.get("namespace", ""),
            "uid": regarding.get("uid", ""),
        },
        "note": event_dict.get("note", "") or event_dict.get("message", ""),
        "reportingController": reporting,
        "timestamp": format_timestamp(ts),
        "cluster": CLUSTER,
    }


def publish_kafka(payload: dict) -> bool:
    producer = get_kafka_producer()
    if producer is None:
        return False
    regarding = payload.get("regarding", {})
    key = f"{regarding.get('kind', 'unknown')}/{regarding.get('name', 'unknown')}"
    try:
        future = producer.send(KAFKA_TOPIC, key=key, value=payload)
        future.get(timeout=10)
        logger.info(
            "Published event %s reason=%s to Kafka topic %s",
            payload["event_uid"],
            payload["reason"],
            KAFKA_TOPIC,
        )
        return True
    except Exception as exc:
        logger.error("Failed to publish event %s to Kafka: %s", payload["event_uid"], exc)
        return False


def post_event_stream(payload: dict) -> bool:
    if not EVENT_STREAM_URL or not EVENT_STREAM_TOKEN:
        return False
    headers = {
        "Content-Type": "application/json",
        "Authorization": EVENT_STREAM_TOKEN,
    }
    event_uid = payload["event_uid"]
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(
                EVENT_STREAM_URL,
                headers=headers,
                json=payload,
                timeout=30,
                verify=False,
            )
            response.raise_for_status()
            logger.info("Forwarded event %s reason=%s to EDA stream", event_uid, payload["reason"])
            return True
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                delay = BACKOFF_SECONDS[attempt]
                logger.warning(
                    "EDA retry %d/%d for event %s after %ss: %s",
                    attempt + 1,
                    MAX_RETRIES,
                    event_uid,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Failed to forward event %s to EDA after %d retries: %s",
                    event_uid,
                    MAX_RETRIES,
                    exc,
                )
    return False


def publish_event(payload: dict) -> None:
    kafka_ok = publish_kafka(payload)
    stream_ok = post_event_stream(payload)
    if not kafka_ok and not stream_ok:
        logger.error("Event %s not delivered to any sink", payload["event_uid"])


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, _format, *_args):
        pass


def start_health_server(port: int) -> None:
    server = HTTPServer(("", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health check listening on port %d", port)


def process_event_dict(event_dict: dict, dedup: LRUCache) -> None:
    if not event_matches_dict(event_dict):
        return
    uid = event_dict.get("metadata", {}).get("uid", "")
    if not uid or not dedup.add(uid):
        return
    publish_event(normalize_event_dict(event_dict))


def watch_events_raw(api_client: client.ApiClient, dedup: LRUCache) -> None:
    resource_version = ""
    while True:
        try:
            url = "/apis/events.k8s.io/v1/events"
            params = {"watch": "true", "timeoutSeconds": str(WATCH_TIMEOUT)}
            if resource_version:
                params["resourceVersion"] = resource_version

            logger.info("Starting watch from resourceVersion=%s", resource_version or "(latest)")
            header_params = {}
            api_client.update_params_for_auth(header_params, {}, ["BearerToken"])
            resp = api_client.rest_client.GET(
                api_client.configuration.host + url,
                query_params=params,
                headers=header_params,
                _preload_content=False,
            )

            buf = ""
            saw_event = False
            for data in resp.stream(amt=4096):
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = raw.get("type", "")
                    obj = raw.get("object", {})
                    rv = obj.get("metadata", {}).get("resourceVersion", "")

                    if event_type == "BOOKMARK":
                        if rv:
                            resource_version = rv
                        continue
                    if event_type not in ("ADDED", "MODIFIED"):
                        continue

                    saw_event = True
                    if rv:
                        resource_version = rv
                    process_event_dict(obj, dedup)

            if not saw_event and resource_version:
                resource_version = ""
            time.sleep(RECONNECT_BACKOFF)

        except ApiException as exc:
            if exc.status == 410:
                resource_version = ""
            else:
                logger.error("Kubernetes API error: %s", exc)
            time.sleep(RECONNECT_BACKOFF)
        except Exception as exc:
            logger.warning("Watch disconnected: %s; reconnecting in %ds", exc, RECONNECT_BACKOFF)
            time.sleep(RECONNECT_BACKOFF)


def main() -> None:
    if not KAFKA_ENABLED and (not EVENT_STREAM_URL or not EVENT_STREAM_TOKEN):
        logger.error("At least one sink required: Kafka or EDA event stream")
        sys.exit(1)

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    start_health_server(HEALTH_PORT)
    cfg = client.Configuration.get_default_copy()
    api_client = client.ApiClient(configuration=cfg)
    dedup = LRUCache(DEDUP_CACHE_SIZE)
    sinks = []
    if KAFKA_ENABLED:
        sinks.append(f"kafka://{KAFKA_BOOTSTRAP_SERVERS}/{KAFKA_TOPIC}")
    if EVENT_STREAM_URL:
        sinks.append(EVENT_STREAM_URL)
    logger.info("Starting event forwarder cluster=%s sinks=%s", CLUSTER, sinks)
    watch_events_raw(api_client, dedup)


if __name__ == "__main__":
    main()
