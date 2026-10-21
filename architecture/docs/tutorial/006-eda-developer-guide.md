# Developer Guide: Extending Platform EDA

**Audience**: Operator and platform developers  
**Reference implementation**: Entity operator  
**Last updated**: 2026-06-09

---

## Prerequisites

- Familiarity with Ansible Operator SDK and `hybridsovereign.redhat` CRDs
- Access to build and push images to the private OCI registry
- Local or CI environment with `ansible-builder` and `podman`
- Understanding of the status handshake protocol (not published in this repository)

---

## Architecture Summary

Each operator requires coordinated changes in three locations:

| Location | Purpose |
|----------|---------|
| `<Operator>/operator/` | Event-only reconcile + finalizer |
| `eda/<operator>/` | Rulebooks, roles, Decision Environment |
| `bootstrap/ansible/roles/eda-config/` | DE and activation registration |

---

## Step 1 — Create Operator Scaffold (Event-Only Template)

Refactor the operator role to emit events instead of provisioning. Use Entity as the template.

### watches.yaml

Enable annotation watching for dashboard refresh:

```yaml
- version: v1alpha1
  group: hybridsovereign.redhat
  kind: Entity
  role: entity
  maxConcurrentReconciles: 10
  reconcileInterval: 300s
  watchAnnotationsChanges: true
  finalizer:
    name: entities.hybridsovereign.redhat/finalizer
    role: entity_finalizer
```

### Main reconcile role (`Entity/operator/roles/entity/tasks/main.yml`)

The event-only pattern:

1. Read CR facts including `observedGeneration`, `ready`, `status`
2. Detect `reconcileNow` annotation
3. Skip if already processed (`observedGeneration >= generation` and `ready: true`)
4. Set `status: reconciling` with `AwaitingEDA` condition
5. Emit `events.k8s.io/v1` Event with spec JSON in `note`
6. Clear `reconcileNow` annotation when set

### Shrink ClusterRole

Remove verbs for external systems (Keycloak, Vault, namespace create). Keep:

- CR read/update/status on your kind
- `events.k8s.io/events` create
- Leases for leader election

### Finalizer role

On delete:

1. Emit `<Kind>DeleteRequested` event
2. `fail` until `status.deletionComplete == true`
3. Exit successfully so SDK removes finalizer

---

## Step 2 — Define Event Reasons

Follow the naming convention from event-contract.md (not published in this repository):

| Action | Reason Pattern | Entity Example |
|--------|----------------|----------------|
| Create/Update | `<Kind>CreateRequested` | `EntityCreateRequested` |
| Delete | `<Kind>DeleteRequested` | `EntityDeleteRequested` |
| Forced reconcile | `<Kind>ReconcileRequested` | `EntityReconcileRequested` |

**Event fields** (required):

| Field | Value |
|-------|-------|
| `regarding.apiVersion` | `hybridsovereign.redhat/v1alpha1` |
| `regarding.kind` | Your CR kind |
| `regarding.uid` | CR metadata UID |
| `reason` | One of the patterns above |
| `action` | `Create`, `Delete`, or `Reconcile` |
| `note` | `spec | to_json` |
| `reportingController` | `<operator-name>-operator` |
| `type` | `Normal` |

The event forwarder filters on `reportingController` matching `*-operator` and `reason` ending in `Requested`.

---

## Step 3 — Create EDA Roles (Provision + Teardown)

Create the directory structure:

```text
eda/entity/
├── roles/
│   ├── entity_provision/
│   │   ├── tasks/main.yml
│   │   └── vars/main.yml
│   └── entity_teardown/
│       └── tasks/main.yml
```

### Provision role pattern

Every provision role starts with cross-cluster connection setup:

```yaml
- name: Parse event payload
  ansible.builtin.set_fact:
    entity_cr_name: "{{ event_payload.regarding.name }}"
    entity_cr_namespace: "{{ event_payload.regarding.namespace }}"
    entity_spec: "{{ event_payload.note | from_json }}"

- name: Read services cluster API secret
  kubernetes.core.k8s_info:
    api_version: v1
    kind: Secret
    name: argocd-cluster-services
    namespace: openshift-gitops
  register: argocd_cluster_secret
  no_log: true

- name: Extract services cluster connection details
  ansible.builtin.set_fact:
    services_bearer_token: >-
      {{ (argocd_cluster_secret.resources[0].data.config | b64decode | from_json).bearerToken }}
    services_api_host: >-
      {{ (argocd_cluster_secret.resources[0].data.config | b64decode | from_json).server }}
  no_log: true
```

Then migrate your operator's provisioning tasks (namespace creation, RBAC, external API calls).

End with a status patch:

```yaml
- name: Mark Entity as ready
  kubernetes.core.k8s:
    state: patched
    api_version: hybridsovereign.redhat/v1alpha1
    kind: Entity
    name: "{{ entity_cr_name }}"
    namespace: "{{ entity_cr_namespace }}"
    definition:
      status:
        status: ready
        ready: true
        observedGeneration: "{{ entity_generation }}"
        conditions:
          - type: Ready
            status: "True"
            reason: EDASucceeded
            message: Entity provisioned by EDA
```

### Teardown role pattern

1. Same cross-cluster connection block
2. Delete external resources (idempotent; tolerate 404)
3. Set `deletionComplete: true` on CR status

---

## Step 4 — Create Decision Environment

Create `eda/entity/decision-environment.yml`:

```yaml
---
version: 3
images:
  base_image:
    name: "registry.redhat.io/ansible-automation-platform-25/de-minimal-rhel9:latest"
dependencies:
  galaxy:
    collections:
      - name: kubernetes.core
        version: ">=3.0.0"
      - name: ansible.controller
        version: ">=4.6.0"
  python:
    - kubernetes>=28.1.0
  system: []
options:
  package_manager_path: /usr/bin/microdnf
```

**Rules**:

- Use the Red Hat DE minimal base image
- Include only collections your roles need
- Do not embed secrets or credentials in the image
- Do not add `ansible.eda` (pre-installed in base)

Create `eda/entity/Makefile`:

```makefile
IMAGE_REGISTRY ?= <your-registry>
IMAGE_NAMESPACE ?= hybrid-sovereign
DE_NAME = de-entity
DE_TAG ?= 0.1.0

de-entity-build:
	ansible-builder build -f decision-environment.yml -t $(IMAGE_REGISTRY)/$(IMAGE_NAMESPACE)/$(DE_NAME):$(DE_TAG)

de-entity-push:
	podman push $(IMAGE_REGISTRY)/$(IMAGE_NAMESPACE)/$(DE_NAME):$(DE_TAG)

de-entity-build-push: de-entity-build de-entity-push
```

Build and push:

```bash
make -C eda/entity de-entity-build-push
```

---

## Step 5 — Create Rulebooks

Create rulebooks under `eda/entity/rulebooks/`.

### entity-create.yml

```yaml
---
- name: Entity Create Rulebook
  hosts: all
  sources:
    - ansible.eda.event_stream:
        event_stream_name: sovereign-operator-events
  rules:
    - name: Handle Entity Create
      condition: event.payload.reason == "EntityCreateRequested" and event.payload.regarding.kind == "Entity"
      action:
        run_playbook:
          name: entity-provision-playbook.yml
          extra_vars:
            event_payload: "{{ event.payload }}"
```

### entity-delete.yml

```yaml
---
- name: Entity Delete Rulebook
  hosts: all
  sources:
    - ansible.eda.event_stream:
        event_stream_name: sovereign-operator-events
  rules:
    - name: Handle Entity Delete
      condition: event.payload.reason == "EntityDeleteRequested" and event.payload.regarding.kind == "Entity"
      action:
        run_playbook:
          name: entity-teardown-playbook.yml
          extra_vars:
            event_payload: "{{ event.payload }}"
```

### Playbook wrappers

`entity-provision-playbook.yml`:

```yaml
---
- name: Entity Provision Playbook
  hosts: localhost
  connection: local
  gather_facts: false
  roles:
    - entity_provision
```

Create matching teardown playbook calling `entity_teardown` role.

Push rulebooks to the `sovereign-eda-rulebooks` Gitea repository.

---

## Step 6 — Register Activation

Add your operator to `bootstrap/ansible/roles/eda-config/tasks/main.yml`:

```yaml
- name: entity
  de_image: "{{ de_registry }}/de-entity:{{ de_tag }}"
  rulebooks:
    - { name: "entity-create", file: "entity-create.yml" }
    - { name: "entity-delete", file: "entity-delete.yml" }
```

The eda-config Job registers:

1. Decision Environment (`de-entity`)
2. Rulebook activations (`entity-create-activation`, `entity-delete-activation`)
3. Binding to Event Stream `sovereign-operator-events`

Deploy via GitOps: enable `sovereignJobs.jobs.edaConfig` in `bootstrap/helm/central/values.yaml`, commit, push, sync.

---

## Step 7 — Test Create + Delete Flows

### Create flow

```bash
# Apply a test Entity CR
oc apply -f Entity/operator/config/samples/hybridsovereign_v1alpha1_entity.yaml

# Watch operator emit event
oc get events -n sovereign-cloud --field-selector reason=EntityCreateRequested

# Verify EDA processed (check CR status)
oc get entity <name> -n sovereign-cloud -o jsonpath='{.status}'

# Expected: status=ready, ready=true, observedGeneration matches metadata.generation
```

### Delete flow

```bash
oc delete entity <name> -n sovereign-cloud

# CR should remain while finalizer waits
oc get entity <name> -n sovereign-cloud

# After EDA teardown: deletionComplete=true, then CR removed
```

### Forced reconcile

Use the tenancy dashboard Refresh button, or manually patch:

```bash
oc patch entity <name> -n sovereign-cloud --type=merge \
  -p '{"metadata":{"annotations":{"ansible.sdk.operatorframework.io/reconcileNow":"true"}}}'
```

### Global test suite

```bash
cd global_tests
ansible-playbook playbooks/validate-eda.yml
```

The `check_eda_events` role performs end-to-end validation with cleanup.

---

## Checklist for New Operators

| # | Task | Done |
|---|------|------|
| 1 | Refactor operator role to event-only | ☐ |
| 2 | Add `watchAnnotationsChanges: true` | ☐ |
| 3 | Shrink ClusterRole (drop external API verbs) | ☐ |
| 4 | Define Create/Delete/Reconcile event reasons | ☐ |
| 5 | Create `eda/<op>/roles/<op>_provision/` | ☐ |
| 6 | Create `eda/<op>/roles/<op>_teardown/` | ☐ |
| 7 | Create `decision-environment.yml` + Makefile | ☐ |
| 8 | Create create + delete rulebooks | ☐ |
| 9 | Push DE image and rulebooks | ☐ |
| 10 | Register in eda-config role | ☐ |
| 11 | Test create, delete, and refresh flows | ☐ |
| 12 | Update both dashboards if status fields changed | ☐ |

---

## Related Documentation

- [006 EDA Architecture](../technical/006-eda-architecture.md)
- [006 EDA Overview](../concepts/006-eda-overview.md)
- [006 Hardening Check](../../hardeningcheck/006-platform-eda-rebuild.md)
- [Entity Operator](../archive/technical/17-entity-operator.md)
- Event contract: event-contract.md (not published in this repository)
