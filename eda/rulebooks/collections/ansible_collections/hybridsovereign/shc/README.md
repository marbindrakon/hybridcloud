# hybridsovereign.shc

Status contract of the `hybridsovereign.redhat` API.

- `hybridsovereign.shc.cr_status` (module): reads an object and writes its `/status` subresource with a
  JSON merge patch guarded by `metadata.resourceVersion`. Conditions are merged by `type` (other types
  are kept, `lastTransitionTime` changes only when the status changes), the legacy types `Running`,
  `Successful` and `Failure` are dropped, a `Ready` condition is derived from a legacy body
  (`status: ready|failed|pending|reconciling`), and `status.phase`, `status.phaseSince` and
  `status.summary` are derived on every write.
- Filters `shc_merge_conditions`, `shc_phase`, `shc_summary`.

`status.phase` is one of `Waiting`, `Creating`, `Updating`, `Ready`, `Degraded`, `Failed`, `Deleting`.
It is derived, never read by a controller. `status.summary` is a short sentence safe to show to the
tenant; the condition `message` carries the detail.

The pure library (`plugins/module_utils/shc_status.py`, `shc_status_reasons.py`) has no Ansible
imports and is also used outside Ansible.
