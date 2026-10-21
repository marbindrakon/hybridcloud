# C4 Level 3 — Event System

**Scope**: AMQ Streams, Event Forwarder, AAP EDA  
**API group**: `hybridsovereign.redhat/v1alpha1` (event `regarding.kind`)  
**Last updated**: 2026-08-25

---

## Purpose

Operators on the services cluster publish normalized event JSON directly to AMQ Streams on the central cluster. Central EDA rulebooks consume from Kafka and dispatch Ansible playbooks via AAP Controller.

1. **Operators** publish to Kafka topic `hybridsovereign-events` (SASL_SSL via external bootstrap Route).
2. **AMQ Streams** (Kafka) provides the durable event bus (12 partitions on `hybridsovereign-events`, 6 on audit).
3. **AAP EDA** matches events via `ansible.eda.kafka` rulebook activations with tuned consumer settings (`KAFKA_MAX_POLL_RECORDS=500`, `KAFKA_FETCH_MAX_WAIT_MS=500`).
4. **Event Forwarder** (optional, `eventForwarder.enabled: true`) bridges Kubernetes Events to the EDA Event Stream for audit visibility.

Primary path remains **operator → Kafka → EDA**. Event Forwarder supplements K8s-native events.

---

## Component Diagram

```mermaid
C4Component
    title Event System — Kafka Pipeline

    Container_Boundary(services, "Services Cluster") {
        Component(operators, "Operators", "Ansible + kafka-python", "Publish *Requested events")
    }

    Container_Boundary(central, "Central Cluster") {
        Component(kafka, "AMQ Streams", "Strimzi Kafka", "hybridsovereign-events topic")
        Component(activations, "Rulebook Activations", "EDA", "ansible.eda.kafka source")
        Component(aap, "AAP Controller", "Job templates", "Execute Ansible roles")
        Component(des, "Decision Environments", "Container images", "Per-domain EE images")
    }

    ComponentDb(vault, "Vault", "amq-producer / amq-consumer credentials")

    Rel(operators, kafka, "Produce SASL_SSL")
    Rel(kafka, activations, "Consume")
    Rel(activations, aap, "run_playbook / run_job_template")
    Rel(aap, des, "Launch in EE")
    Rel(operators, vault, "Producer creds via ExternalSecret")
    Rel(activations, vault, "Consumer creds via EDA credential")
```

---

## Operator Kafka Producer

**Path**: `hybridcloud/operator/roles/_common/tasks/amq_publish.yml`  
**Credentials**: ExternalSecret `amq-producer-credentials` → Vault `central/data/amq-producer`

Payload schema matches the historical forwarder normalization: `reason`, `regarding`, `note` (JSON spec snapshot).

---

## AMQ Streams

**Spec**: `hybridcloud/specs/016-amq-streams/spec.md`  
**Namespace**: `amq-streams` (central cluster)  
**Sync-wave**: 13

| Resource | Name | Config |
|----------|------|--------|
| Kafka cluster | `hybridsovereign-kafka` | 3 brokers, 3 ZooKeeper |
| Topic | `hybridsovereign-events` | Operator and platform events |
| Topic | `hybridsovereign-audit` | Audit trail (reserved) |

TLS and mutual authentication protect broker access. Producers and consumers authenticate via SASL credentials from Vault.

---

## AAP EDA

**Config-as-code**: `hybridcloud/aap-config/eda/`  
**Rulebooks**: `hybridcloud/eda/*/rulebooks/`  
**Common roles**: `hybridcloud/eda/common/` and `hybridcloud/eda/rulebooks/roles/`

### Event Stream

```yaml
# hybridcloud/aap-config/eda/event_streams.yml
eda_event_streams:
  - name: sovereign-operator-events
    forward_events: true
```

### Activation Pattern

Each CR kind has create and delete activations (34 activations total). Example:

```yaml
# hybridcloud/aap-config/eda/rulebook_activations.yml (excerpt)
- name: entity-create-activation
  rulebook: entity-create.yml
  decision_environment: de-entity
  event_streams:
    - event_stream: sovereign-operator-events
```

### Rulebook Matching

```yaml
# hybridcloud/eda/entity/rulebooks/entity-create.yml
condition: >-
  event.payload.reason in ["EntityCreateRequested", "EntityReconcileRequested"]
  and event.payload.regarding.kind == "Entity"
action:
  run_playbook:
    name: entity-provision-playbook.yml
```

---

## Event Contract

| Reason pattern | Example | When emitted |
|----------------|---------|--------------|
| `<Kind>CreateRequested` | `EntityCreateRequested` | CR created or `observedGeneration` stale |
| `<Kind>DeleteRequested` | `EntityDeleteRequested` | `deletionTimestamp` set |
| `<Kind>ReconcileRequested` | `EntityReconcileRequested` | `reconcileNow` annotation |

### Normalized Forwarder Payload

| Field | Purpose |
|-------|---------|
| `regarding.kind` | CR kind for rulebook matching |
| `regarding.name` | CR name |
| `regarding.namespace` | CR namespace |
| `reason` | Event reason string |
| `note` | Compact JSON spec snapshot (max 1024 chars) |

---

## End-to-End Sequence

```mermaid
sequenceDiagram
    participant U as User
    participant CR as Entity CR
    participant PO as Primary Operator
    participant EV as K8s Event
    participant EF as Event Forwarder
    participant KF as Kafka
    participant ES as EDA Event Stream
    participant EDA as EDA Activation
    participant AAP as AAP Controller

    U->>CR: Create Entity
    CR->>PO: Reconcile
    PO->>CR: status=reconciling
    PO->>EV: EntityCreateRequested
    EV->>EF: Watch delivers event
    EF->>KF: Produce to hybridsovereign-events
    EF->>ES: POST normalized payload
    ES->>EDA: Match rule
    EDA->>AAP: run_playbook entity-provision
    AAP->>CR: Patch status=ready
```

---

## Decision Environments

Each domain has a dedicated Decision Environment image built from `hybridcloud/eda/<domain>/`:

| DE | Domain | Path |
|----|--------|------|
| `de-entity` | Entity lifecycle | `hybridcloud/eda/entity/` |
| `de-team` | Team management | `hybridcloud/eda/team/` |
| `de-assignment` | Assignment linking | `hybridcloud/eda/assignment/` |
| `de-plugin-rbac` | RBAC plugin | `hybridcloud/eda/plugin-rbac/` |
| `de-plugin-vault` | Vault plugin | `hybridcloud/eda/plugin-vault/` |
| `de-openstack-migration` | VM migration | `hybridcloud/eda/openstack-migration/` |

Images are pushed to Quay and referenced in `hybridcloud/aap-config/eda/decision_environments.yml`.

---

## Related Documents

- [operator.md](operator.md) — event-emitting operators
- [../decisions/ADR-003-kafka-events.md](../../decisions/ADR-003-kafka-events.md)
- [../technical/006-eda-architecture.md](../../technical/006-eda-architecture.md)
