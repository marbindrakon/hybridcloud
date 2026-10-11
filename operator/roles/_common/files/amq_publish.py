#!/usr/bin/env python3
"""Publish a hybridsovereign operator event to AMQ Streams (Kafka)."""
import json
import os
import ssl
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from kafka import KafkaProducer


def main() -> int:
    payload = json.loads(os.environ["AMQ_EVENT_PAYLOAD"])
    bootstrap = os.environ["KAFKA_BOOTSTRAP_SERVERS"]
    topic = os.environ.get("KAFKA_TOPIC", "hybridsovereign-events")
    username = os.environ.get("KAFKA_USERNAME", "")
    password = os.environ.get("KAFKA_PASSWORD", "")

    kwargs = {
        "bootstrap_servers": bootstrap.split(","),
        "value_serializer": lambda v: json.dumps(v).encode("utf-8"),
        "key_serializer": lambda k: k.encode("utf-8") if k else None,
        "acks": "all",
        "retries": 5,
        "request_timeout_ms": 15000,
    }

    if username and password:
        kwargs["security_protocol"] = os.environ.get("KAFKA_SECURITY_PROTOCOL", "SASL_SSL")
        kwargs["sasl_mechanism"] = os.environ.get("KAFKA_SASL_MECHANISM", "SCRAM-SHA-512")
        kwargs["sasl_plain_username"] = username
        kwargs["sasl_plain_password"] = password
        # shc (LAB-PLAN §11.1/§11.2): verify the brokers. The external routes serve publicly trusted
        # certificates for the bootstrap and broker hosts, so the default trust store applies;
        # KAFKA_CA_CERT (PEM) is added for clusters whose routes pass through to the Strimzi CA.
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ca_pem = os.environ.get("KAFKA_CA_CERT", "").strip()
        if ca_pem.startswith("-----BEGIN CERTIFICATE-----"):
            ctx.load_verify_locations(cadata=ca_pem)
        kwargs["ssl_context"] = ctx

    producer = KafkaProducer(**kwargs)
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
