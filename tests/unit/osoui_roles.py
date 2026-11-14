"""Shared set-up of the role tests of OpenStack sign-in and cluster users (po_harness.py runs the task files):
  - the real helper (files/shc_openstack.py) for the Keystone actions, against fake_keystone.Keystone over HTTP;
    the other OpenStack actions (Neutron, Nova, Designate) answer from fake_os as in po_harness;
  - the real Vault task files (vault_kv_read / write / delete) against fake_vault.Vault over HTTP;
  - the sign-in directory side (common/tasks/services_signin_groups.yml) replaced by a trace that answers
    ssg_released / ssg_all_role_names from fake_ssg (a test of the directory side itself is not this module's).
"""
import os
import tempfile

import sys
sys.path.insert(0, os.path.dirname(__file__))
import fake_keystone  # noqa: E402
import fake_vault  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REAL_CALL = open(os.path.join(ROOT, "eda", "common", "tasks", "openstack_call.yml")).read()

TRACE = """- name: trace
  ansible.builtin.shell: cat >> {{ trace_file }}
  args:
    stdin: "{{ %s | to_json }}"
  changed_when: false
"""

HYBRID_CALL = TRACE % ("{'os': os_request, 'as': fake_account_loaded | default(''), 'cacert': os_account_cacert | default('')}") + """
- name: fake result
  ansible.builtin.set_fact:
    os_result: >-
      {{ fake_os[os_request.action ~ '/' ~ (os_request.state | default(''))]
         | default(fake_os[os_request.action] | default({})) }}
  when: (os_request.action ~ '/' ~ (os_request.state | default(''))) in fake_os or os_request.action in fake_os

- name: the real helper
  ansible.builtin.include_tasks: openstack_call_real.yml
  when: not ((os_request.action ~ '/' ~ (os_request.state | default(''))) in fake_os or os_request.action in fake_os)

- name: trace the helper's answer
  ansible.builtin.shell: cat >> {{ trace_file }}
  args:
    stdin: "{{ {'os_result': os_result} | to_json }}"
  changed_when: false
"""

FAKE_SSG = TRACE % ("{'ssg': {'owner': ssg_owner, 'entity': ssg_entity, 'wanted': ssg_wanted | map(attribute='group') "
                    "| list, 'realm': entity_signin_realm | default(''), 'client': entity_signin_client | default(''), "
                    "'rbacConfig': entity_signin_rbac_config | default(''), "
                    "'description': ssg_role_description | default('')}}") + """
- name: fake directory answer
  vars:
    _all: "{{ (fake_ssg | default({})).all | default([]) }}"
    _owned: "{{ (fake_ssg | default({})).owned | default({}) }}"
    _wanted: "{{ ssg_wanted | map(attribute='group') | list }}"
  ansible.builtin.set_fact:
    ssg_all_role_names: "{{ _all }}"
    ssg_released: >-
      {%- set out = [] -%}
      {%- for name, owners in _owned.items() if ssg_owner in owners and name not in _wanted -%}
        {%- set _ = out.append({'key': name, 'value': {'owners': owners | reject('equalto', ssg_owner) | list}}) -%}
      {%- endfor -%}
      {{ out }}
"""

FAKES = {"openstack_call.yml": HYBRID_CALL, "openstack_call_real.yml": REAL_CALL.split("---\n", 1)[1],
         "services_signin_groups.yml": FAKE_SSG}


class Site:
    """A fake Keystone with a tenant domain, an admin site account (admin on the tenant domain, as the probe gives
    it) and a Vault; env() is the environment the play runs with."""

    def __init__(self, granularity=1.0, manager=False):
        self.tmp = tempfile.mkdtemp(prefix="shc-osoui-role-")
        self.ks = fake_keystone.Keystone(granularity=granularity).start()
        self.vault = fake_vault.Vault().start()
        self.tenants = self.ks.add_domain("tenants")
        if manager:
            uid, pw, _ = self.ks.site_account(roles=("manager",), domain_scope=self.tenants, name="site-manager")
            self.account = self.ks.clouds_yaml(os.path.join(self.tmp, "account.yaml"), "site-manager", pw,
                                               project="site-manager-project", domain="tenants")
        else:
            uid, pw, _ = self.ks.site_account()
            self.ks.grant("user", uid, "domain", self.tenants, "admin")
            self.account = self.ks.clouds_yaml(os.path.join(self.tmp, "account.yaml"), "site-admin", pw,
                                               project="site-admin-project")
        self.account_user, self.account_password = uid, pw
        empty = os.path.join(self.tmp, "no-clouds.yaml")
        with open(empty, "w") as f:
            f.write("clouds: {}\n")
        self._env = {"VAULT_ADDR": self.vault.url, "VAULT_TOKEN": self.vault.token, "OS_CLIENT_CONFIG_FILE": empty,
                     "PYTHONDONTWRITEBYTECODE": "1"}

    def env(self):
        return self._env

    def stop(self):
        self.ks.stop()
        self.vault.stop()
