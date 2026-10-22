#!/usr/bin/env python3
"""Publish a hybridsovereign operator event to AMQ Streams (Kafka)."""
import json
import os
import ssl
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from kafka import KafkaProducer


# Kafka protocol version the producer speaks. Pinned so the producer does not probe the broker version first: the
# probe has a short timeout and one slow DNS resolver made every publish fail although the next resolver answered.
# KAFKA_API_VERSION overrides it ("auto" probes). kafka-python 3.x has no api_version_auto_timeout_ms (it rejects
# unknown configs); when probing, the 10 s probe timeout is passed only to a client that knows the option.
DEFAULT_API_VERSION = "2.6.0"
API_VERSION_PROBE_MS = 10000
PROBE_TIMEOUT_KEY = "api_version_auto_timeout_ms"


def api_version(value):
    value = (value or "").strip()
    if value.lower() == "auto":
        return None
    try:
        return tuple(int(p) for p in (value or DEFAULT_API_VERSION).split("."))
    except ValueError:
        return tuple(int(p) for p in DEFAULT_API_VERSION.split("."))


def producer_kwargs(environ, known_configs):
    """KafkaProducer keyword arguments from the environment; known_configs is KafkaProducer.DEFAULT_CONFIG."""
    bootstrap = environ["KAFKA_BOOTSTRAP_SERVERS"]
    username = environ.get("KAFKA_USERNAME", "")
    password = environ.get("KAFKA_PASSWORD", "")

    kwargs = {
        "bootstrap_servers": bootstrap.split(","),
        "value_serializer": lambda v: json.dumps(v).encode("utf-8"),
        "key_serializer": lambda k: k.encode("utf-8") if k else None,
        "acks": "all",
        "retries": 5,
        "request_timeout_ms": 15000,
        "api_version": api_version(environ.get("KAFKA_API_VERSION", "")),
    }
    if kwargs["api_version"] is None and PROBE_TIMEOUT_KEY in known_configs:
        kwargs[PROBE_TIMEOUT_KEY] = API_VERSION_PROBE_MS

    if username and password:
        kwargs["security_protocol"] = environ.get("KAFKA_SECURITY_PROTOCOL", "SASL_SSL")
        kwargs["sasl_mechanism"] = environ.get("KAFKA_SASL_MECHANISM", "SCRAM-SHA-512")
        kwargs["sasl_plain_username"] = username
        kwargs["sasl_plain_password"] = password
        # shc (LAB-PLAN §11.1/§11.2): verify the brokers. The external routes serve publicly trusted
        # certificates for the bootstrap and broker hosts, so the default trust store applies;
        # KAFKA_CA_CERT (PEM) is added for clusters whose routes pass through to the Strimzi CA.
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ca_pem = environ.get("KAFKA_CA_CERT", "").strip()
        if ca_pem.startswith("-----BEGIN CERTIFICATE-----"):
            ctx.load_verify_locations(cadata=ca_pem)
        kwargs["ssl_context"] = ctx
    return kwargs


def main() -> int:
    payload = json.loads(os.environ["AMQ_EVENT_PAYLOAD"])
    topic = os.environ.get("KAFKA_TOPIC", "hybridsovereign-events")
    producer = KafkaProducer(**producer_kwargs(os.environ, KafkaProducer.DEFAULT_CONFIG))
    regarding = payload.get("regarding", {})
    key = "{}/{}".format(regarding.get("kind", "unknown"), regarding.get("name", "unknown"))
    # ansible.eda.kafka puts the message JSON under event.body.
    # Wrap so rulebooks can match event.body.payload.reason / regarding.
    message = payload if "payload" in payload else {"payload": payload}
    future = producer.send(topic, key=key, value=message)
    future.get(timeout=30)
    producer.flush(timeout=30)
    producer.close(timeout=10)
    print("published", key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
