# AAP Job Template Catalog

**Feature**: EDA → AAP Job Template Architecture  
**Audience**: Platform engineers, operator developers  
**Last updated**: 2026-06-15

---

## Overview

All provisioning and teardown operations are executed as AAP Controller job templates. EDA activations receive Kubernetes events and call `run_job_template` — the heavy Ansible logic runs inside AAP's managed Execution Environments.

**AAP Controller URL**: `https://sovereign-aap-controller-aap.apps.central.lab.example.com`  
**Organisation**: `sovereign`  
**Project**: `sovereign-eda-project` (mirrors Gitea `eda/` repo)  
**Inventory**: `sovereign-localhost` (single `localhost`, `ansible_connection=local`)

---

## Job Template Catalog

| Template Name | Operator | EE Image | Playbook |
|---|---|---|---|
| `entity-provision` | Entity | `de-entity:0.1.6` | `rulebooks/entity-provision-playbook.yml` |
| `entity-teardown` | Entity | `de-entity:0.1.6` | `rulebooks/entity-teardown-playbook.yml` |
| `team-provision` | Team | `de-entity:0.1.6` | `rulebooks/team-provision-playbook.yml` |
| `team-teardown` | Team | `de-entity:0.1.6` | `rulebooks/team-teardown-playbook.yml` |
| `project-provision` | Project | `de-entity:0.1.6` | `rulebooks/project-provision-playbook.yml` |
| `project-teardown` | Project | `de-entity:0.1.6` | `rulebooks/project-teardown-playbook.yml` |
| `persona-provision` | Persona | `de-persona:0.1.2` | `rulebooks/persona-provision-playbook.yml` |
| `persona-teardown` | Persona | `de-persona:0.1.2` | `rulebooks/persona-teardown-playbook.yml` |
| `assignment-provision` | Assignment | `de-assignment:0.1.4` | `rulebooks/assignment-provision-playbook.yml` |
| `assignment-teardown` | Assignment | `de-assignment:0.1.4` | `rulebooks/assignment-teardown-playbook.yml` |
| `cloudaws-provision` | CloudAWS | `de-cloudaws:0.1.4` | `rulebooks/cloudaws-provision-playbook.yml` |
| `cloudaws-teardown` | CloudAWS | `de-cloudaws:0.1.4` | `rulebooks/cloudaws-teardown-playbook.yml` |
| `cloudoso-provision` | CloudOSO | `de-cloudoso:0.1.6` | `rulebooks/cloudoso-provision-playbook.yml` |
| `cloudoso-teardown` | CloudOSO | `de-cloudoso:0.1.6` | `rulebooks/cloudoso-teardown-playbook.yml` |
| `openstack-migration-dummy` | OpenStackMigration | `de-openstack-migration:0.1.0` | `rulebooks/openstack-migration-dummy-playbook.yml` |
| `platformopenshift-provision` | PlatformOpenshift | `de-platformopenshift:0.1.4` | `rulebooks/platformopenshift-provision-playbook.yml` |
| `platformopenshift-teardown` | PlatformOpenshift | `de-platformopenshift:0.1.4` | `rulebooks/platformopenshift-teardown-playbook.yml` |
| `rbacconfig-provision` | RBACConfig | `de-entity:0.1.6` | `rulebooks/rbacconfig-provision-playbook.yml` |
| `rbac-provision` | RBAC | `de-entity:0.1.6` | `rulebooks/rbac-provision-playbook.yml` |
| `vault-provision` | Vault | `de-entity:0.1.6` | `rulebooks/vault-provision-playbook.yml` |
| `vaultkv-provision` | VaultKV | `de-entity:0.1.6` | `rulebooks/vaultkv-provision-playbook.yml` |
| `aapconfig-provision` | AAPConfig | `de-entity:0.1.6` | `rulebooks/aapconfig-provision-playbook.yml` |
| `aaporg-provision` | AAPOrg | `de-entity:0.1.6` | `rulebooks/aaporg-provision-playbook.yml` |
| `quayconfig-provision` | QuayConfig | `de-entity:0.1.6` | `rulebooks/quayconfig-provision-playbook.yml` |
| `quayorg-provision` | QuayOrg | `de-entity:0.1.6` | `rulebooks/quayorg-provision-playbook.yml` |

All templates are configured with `ask_variables_on_launch: true`.

---

## Credentials

| Credential Name | Type | Vault Path | Purpose |
|---|---|---|---|
| `sovereign-vault-token` | HashiCorp Vault (Approle) | `central/vault-services-client` | Vault access for ExternalSecrets |
| `sovereign-aws-creds` | AWS | `central/aws-credentials` | CloudAWS environment provisioning |
| `sovereign-openstack-creds` | OpenStack | `central/openstack-credentials` | CloudOSO provisioning |
| `sovereign-gitea-cred` | Source Control | Gitea admin token | AAP project checkout |
| `sovereign-central-cluster-credential` | OpenShift/K8s Bearer Token | `central/aap-central-cluster-sa` | Central cluster API access |

---

## Execution Environments

Each EE is registered in AAP pointing to `quay.example.com/hybrid-sovereign/<name>:<tag>`:

| EE Name | Image Tag | Collections | Key Python Deps |
|---|---|---|---|
| `de-entity` | 0.1.6 | `kubernetes.core`, `ansible.controller` | `kubernetes>=28.1.0` |
| `de-persona` | 0.1.2 | `kubernetes.core`, `ansible.controller` | `kubernetes>=28.1.0` |
| `de-assignment` | 0.1.4 | `kubernetes.core`, `ansible.controller` | `kubernetes>=28.1.0` |
| `de-cloudaws` | 0.1.4 | `kubernetes.core`, `amazon.aws>=8.0.0` | `kubernetes`, `boto3` |
| `de-cloudoso` | 0.1.6 | `kubernetes.core`, `openstack.cloud`, `amazon.aws>=8.0.0` | `kubernetes`, `openstacksdk` |
| `de-openstack-migration` | 0.1.0 | `kubernetes.core`, `ansible.controller` | `kubernetes>=28.1.0` |
| `de-platformopenshift` | 0.1.4 | `kubernetes.core` | `kubernetes>=28.1.0` |

> **Note**: `de-cloudoso:0.1.6` build requires patching the `ansible-builder`-generated `Containerfile` to remove `openshift-clients` (not available in base image) and `systemd-python` (requires missing `systemd-devel`) from bindep and pip requirements. See `eda/cloudoso/Makefile`.

---

## EDA Rulebook Configuration

### run_job_template Structure

All 24 EDA rulebooks follow this pattern:

```yaml
rules:
  - name: Handle <Kind>CreateRequested
    condition: >
      event.payload.reason == "<Kind>CreateRequested"
      and event.payload.regarding.kind == "<Kind>"
    action:
      run_job_template:
        name: <operator>-provision
        organization: sovereign
        job_args:
          extra_vars:
            event_payload: "{{ event.payload }}"
```

### aap_resource_token

EDA activations receive the `aap_resource_token` (from Vault `central/aap-admin-central`) to authenticate against AAP Controller when calling `run_job_template`. This is passed during activation creation by the `eda-config` role.

---

## CR Status Update Pattern

Each provision role calls `patch_cr_status.yml` twice:

### START (immediately after services cluster credentials are set)

```yaml
- name: Announce AAP job started in CR status
  ansible.builtin.include_tasks: patch_cr_status.yml
  vars:
    cr_status_body:
      status: reconciling
      message: "Job started"
  when: lookup('env', 'TOWER_JOB_ID') | length > 0
```

### END (after provisioning completes)

```yaml
- name: Patch <Kind> CR status
  ansible.builtin.include_tasks: patch_cr_status.yml
  vars:
    cr_status_body:
      status: ready
      ready: true
      observedGeneration: "{{ cr_generation }}"
```

### patch_cr_status.py Logic

The embedded Python script:

1. Reads `TOWER_JOB_ID` and `TOWER_URL` from OS environment (auto-set by AAP runner)
2. Builds AAP job URL: `{tower_url}/execution/jobs/playbook/{tower_job_id}/output`
3. Sets `edaJobs = [single_entry]` — replaces the entire array with 1 entry
4. Falls back to S3 log URL → EDA activation URL if AAP vars not set

```python
tower_job_id = os.environ.get('TOWER_JOB_ID', '')
tower_url = os.environ.get('TOWER_URL', '')
if tower_job_id and tower_url:
    job_url = f"{tower_url}/execution/jobs/playbook/{tower_job_id}/output"
```

---

## AAP Controller Configuration Job

The `aap-controller-config` Kubernetes Job (sync wave 33) runs the `aap-controller-config` Ansible role using `infra.aap_configuration.dispatch` to configure all of the above in a single idempotent run.

**Job spec location**: `bootstrap/helm/central/values.yaml` → `sovereignJobs.jobs.aapControllerConfig`  
**Role location**: `bootstrap/ansible/roles/aap-controller-config/`  
**Playbook**: `bootstrap/ansible/project/aap-controller-config.yml`

To re-run after changes, bump `RUN_ID` in `values.yaml` and push — ArgoCD will recreate the Job.

---

## RBAC Requirements

The `sovereign-aap-controller` service account (namespace `aap`) requires:

| Resource | Namespace | Verbs | Purpose |
|---|---|---|---|
| `secrets` (argocd-cluster-services, eda-s3-creds) | `openshift-gitops` | `get`, `list` | Read ArgoCD cluster creds and S3 creds |
| `externalsecrets` | `sovereign-cloud-jobs` | `get`, `list`, `create`, `update`, `patch`, `delete` | Manage temp ExternalSecrets for cloud creds |
| `secrets` | `sovereign-cloud-jobs` | `get`, `list` | Read synced cloud credential secrets |

Chart: `bootstrap/helm/charts/sovereign-job-rbac/` (current version `0.1.8`)

---

## Troubleshooting

### Job fails immediately (< 15s)

- Check `TOWER_JOB_ID` and `TOWER_URL` env vars are set in the EE
- Check EE image pull succeeded (`oc get pod -n aap -l job-name=...`)

### CR status not updated with AAP job ID

- Verify `patch_cr_status.yml` explicitly sets `TOWER_JOB_ID` and `TOWER_URL` as env vars
- Check the Python script runs with correct environment in the job output

### ExternalSecret not syncing cloud credentials

- Check the `vault-backend` ClusterSecretStore can access the Vault path
- Verify Vault path uses correct keys (`access-key`/`secret-access-key` for Route53, `clouds.yaml` for OpenStack)
- Force refresh: `oc annotate externalsecret <name> -n sovereign-cloud-jobs force-sync=$(date +%s) --overwrite`

### cloudoso fails with `identity:list_projects`

- The `clouds.yaml` stored in Vault at `oso/accounts/<account>` must use `domain_name: <domain>` (not `project_domain_name`) to obtain a domain-scoped token
- Example working clouds.yaml:
  ```yaml
  clouds:
    openstack:
      auth:
        auth_url: https://identity.example.com
        username: admin
        password: <password>
        user_domain_name: shc_domain
        domain_name: shc_domain
      identity_api_version: 3
  ```

---

## Related Documentation

- [006 EDA Architecture](006-eda-architecture.md) — full EDA flow and event contract
- [47 Samples Testing Status](../archive/technical/47-samples-testing-status.md) — current CR testing state
- [Vault and secret delivery](09-vault.md) — Vault and ExternalSecret patterns
