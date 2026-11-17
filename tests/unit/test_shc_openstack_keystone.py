"""The Keystone actions of eda/common/files/shc_openstack.py against a Keystone over HTTP (fake_keystone.py) that
enforces what a site's Keystone was shown to do: a password set revokes the user's earlier tokens, a token issued in
the second of a set is refused, a restricted credential may not make credentials, credentials go with their user,
a domain manager's limits. The helper's own functions are never replaced; openstacksdk / keystoneauth sign the site
account in over HTTP as in a job. Names and ids are examples."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
try:
    import openstack  # noqa: F401
    import requests  # noqa: F401
    HAVE_SDK = True
except ImportError:  # pragma: no cover - the job images have both
    HAVE_SDK = False

import fake_keystone as F  # noqa: E402

TMP = tempfile.mkdtemp(prefix="shc-ks-test-")
# openstacksdk reads clouds.yaml from the working directory and the user's config: none of that here
_EMPTY = os.path.join(TMP, "no-clouds.yaml")
with open(_EMPTY, "w") as _f:
    _f.write("clouds: {}\n")
os.environ["OS_CLIENT_CONFIG_FILE"] = _EMPTY

if HAVE_SDK:
    import shc_openstack as S  # noqa: E402


class Base(unittest.TestCase):
    granularity = 0.1
    settle = 0.25

    def setUp(self):
        self.ks = F.Keystone(granularity=self.granularity).start()
        self.addCleanup(self.ks.stop)
        self.tenants = self.ks.add_domain("tenants")
        self.uid, self.pw, _ = self.ks.site_account()
        self.ks.grant("user", self.uid, "domain", self.tenants, "admin")   # what the site probe gives an admin
        self.clouds = self.ks.clouds_yaml(os.path.join(TMP, f"acct-{id(self)}.yaml"), "site-admin", self.pw,
                                          project="site-admin-project")
        self.pid = self.ks.add_project("acme_env1", self.tenants)
        p = mock.patch.multiple(S, PASSWORD_SETTLE_SECONDS=self.settle, UNIT_RETRY_WAIT=(0.0, 0.05))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.assertEqual(self.ks.faults, [], "the fake Keystone itself failed")

    def manager_account(self):
        uid, pw, _ = self.ks.site_account(roles=("manager",), domain_scope=self.tenants, name="site-manager")
        return self.ks.clouds_yaml(os.path.join(TMP, f"mgr-{id(self)}.yaml"), "site-manager", pw,
                                   project="site-manager-project", domain="tenants")

    def calls(self, method=None, prefix=""):
        return [c for c in self.ks.log if (method is None or c[0] == method) and c[1].startswith(prefix)]


def cluster_req(t, **over):
    out = os.path.join(TMP, f"out-{id(t)}-{len(t.ks.log)}.json")
    req = {"action": "cluster-identity", "clouds_yaml": t.clouds, "domain": "tenants", "project_id": t.pid,
           "user_name": "shc-oso-acme-c1-0a1b2", "description": "User of OpenShift cluster entity-acme/c1",
           "roles": ["member"], "credential_name": "shc-oso-acme-c1-0a1b2",
           "credential_description": "OpenShift cluster entity-acme/c1", "vault_file": "", "out_file": out}
    req.update(over)
    return req


def vault_read_of(out_file, path):
    """What a Vault KV v2 GET answers for the body the helper wrote (the next task writes it to Vault)."""
    with open(out_file) as f:
        body = json.load(f)
    with open(path, "w") as f:
        json.dump({"data": {"data": body["data"], "metadata": {"version": 1}}}, f)
    return path


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class ClusterIdentity(Base):
    def test_user_roles_and_restricted_credential(self):
        req = cluster_req(self)
        out = S.cluster_identity(req)
        user = self.ks.users[out["user"]["id"]]
        self.assertEqual(user["domain_id"], self.tenants)
        self.assertEqual(user["options"], S.USER_OPTIONS)
        self.assertIn(S.MARKER, user["description"])
        self.assertTrue(out["user"]["created"])
        self.assertEqual(out["roles"]["granted"], ["member"])
        self.assertEqual(self.ks.effective_roles(user["id"], "project", self.pid), {"member", "reader"})
        cred = self.ks.app_creds[out["credential"]["id"]]
        self.assertEqual((cred["user"], cred["project"], cred["roles"], cred["unrestricted"], cred["expires_at"]),
                         (user["id"], self.pid, ["member"], False, None))
        self.assertEqual(cred["name"], req["credential_name"])
        # the order of the unit: password set, sign-in as the user, its credentials listed, one made, second set
        seq = [(m, p.split("/")[-1]) for (m, p, _) in self.ks.log[-5:]]
        self.assertEqual(seq, [("PATCH", user["id"]), ("POST", "tokens"), ("GET", "application_credentials"),
                               ("POST", "application_credentials"), ("PATCH", user["id"])])
        # the Vault write body: a private file with the clouds.yaml of the credential
        self.assertEqual(stat.S_IMODE(os.stat(req["out_file"]).st_mode), 0o600)
        import yaml
        with open(req["out_file"]) as f:
            clouds = yaml.safe_load(json.load(f)["data"]["clouds.yaml"])["clouds"]["openstack"]
        self.assertEqual(clouds["auth_type"], "v3applicationcredential")
        self.assertEqual(clouds["auth"]["application_credential_id"], cred["id"])
        self.assertEqual(clouds["auth"]["application_credential_secret"], cred["secret"])
        self.assertEqual(clouds["region_name"], "RegionOne")
        # the credential works, and as restricted it can neither make nor delete credentials nor set a password (a
        # token of the second of the last password set would be refused: the fake's second is short)
        import time
        time.sleep(self.granularity * 2)
        k = S.Keystone(S.Account({"clouds_yaml": self.clouds}))
        st, tok, body = k.app_cred_token(cred["id"], cred["secret"])
        self.assertEqual((st, body["token"]["project"]["id"]), (201, self.pid))
        st, _, _ = k.request(tok, "POST", f"/users/{user['id']}/application_credentials",
                             {"application_credential": {"name": "x"}})
        self.assertEqual(st, 403)
        st, _, _ = k.request(tok, "PATCH", f"/users/{user['id']}", {"user": {"password": "x"}})
        self.assertEqual(st, 403)
        # the password the run signed in with is dead (the second set replaced it; that one is never written)
        used = S._SECRETS[-3]
        self.assertEqual(k.password_token(user["id"], used, self.pid)[0], 401)
        self.assertNotEqual(self.ks.users[user["id"]]["password"], used)

    def test_a_steady_run_keeps_the_credential_and_touches_no_password(self):
        req = cluster_req(self)
        first = S.cluster_identity(req)
        vf = vault_read_of(req["out_file"], os.path.join(TMP, f"vault-{id(self)}.json"))
        n = len(self.ks.log)
        again = S.cluster_identity(cluster_req(self, vault_file=vf))
        self.assertTrue(again["credential"]["kept"])
        self.assertFalse(again["wrote"])
        self.assertEqual(again["credential"]["id"], first["credential"]["id"])
        later = self.ks.log[n:]
        self.assertEqual([c for c in later if c[0] in ("PATCH", "PUT", "DELETE")
                          or (c[0] == "POST" and "application_credentials" in c[1])], [])
        self.assertFalse(again["user"]["created"])

    def test_a_credential_in_vault_that_no_longer_signs_in_is_made_again(self):
        req = cluster_req(self)
        first = S.cluster_identity(req)
        vf = vault_read_of(req["out_file"], os.path.join(TMP, f"vault-{id(self)}.json"))
        del self.ks.app_creds[first["credential"]["id"]]
        again = S.cluster_identity(cluster_req(self, vault_file=vf))
        self.assertFalse(again["credential"]["kept"])
        self.assertTrue(again["wrote"])
        self.assertNotEqual(again["credential"]["id"], first["credential"]["id"])
        self.assertEqual(len([c for c in self.ks.app_creds.values() if c["user"] == again["user"]["id"]]), 1)

    def test_a_credential_of_a_failed_run_is_replaced_not_added(self):
        # a run made the credential but its secret never reached Vault: the next run deletes the user's credentials
        first = S.cluster_identity(cluster_req(self))
        again = S.cluster_identity(cluster_req(self, vault_file=""))
        uid = again["user"]["id"]
        self.assertEqual([c["id"] for c in self.ks.app_creds.values() if c["user"] == uid],
                         [again["credential"]["id"]])
        self.assertNotIn(first["credential"]["id"], self.ks.app_creds)

    def test_a_user_of_that_name_that_is_not_the_platforms_is_refused(self):
        foreign = self.ks.add_user("shc-oso-acme-c1-0a1b2", self.tenants, password="theirs", description="mine")
        with self.assertRaises(S.Refusal) as cm:
            S.cluster_identity(cluster_req(self))
        self.assertEqual(cm.exception.reason, "UserRefused")
        self.assertEqual(cm.exception.message,
                         "The site refused the cluster's user shc-oso-acme-c1-0a1b2: a user named "
                         "shc-oso-acme-c1-0a1b2 exists in domain tenants and is not the platform's. "
                         "The cluster cannot be built.")
        self.assertEqual(self.ks.users[foreign]["password"], "theirs")
        self.assertEqual(self.calls("PATCH") + self.calls("PUT"), [])

    def test_an_existing_user_gets_the_options_back(self):
        uid = self.ks.add_user("shc-oso-acme-c1-0a1b2", self.tenants, description="x. " + S.MARKER,
                               options={"lock_password": False})
        out = S.cluster_identity(cluster_req(self))
        self.assertEqual(out["user"]["id"], uid)
        self.assertEqual(self.ks.users[uid]["options"], S.USER_OPTIONS)

    def test_a_concurrent_password_set_costs_a_retry_not_a_failure(self):
        state = {"done": False}

        def hook(method, path):
            # another job of the same cluster sets the user's password while this one holds a token
            if method == "GET" and path.endswith("/application_credentials") and not state["done"]:
                state["done"] = True
                for u in self.ks.users.values():
                    if u["name"].startswith("shc-oso"):
                        u["password"] = "set-by-another-job"
                        u["revoked_before"] = self.ks.tick()
        self.ks.on_request = hook
        out = S.cluster_identity(cluster_req(self))
        self.assertTrue(state["done"])
        self.assertFalse(out["credential"]["kept"])
        self.assertIn(("GET", f"/v3/users/{out['user']['id']}/application_credentials", 401), self.ks.log)
        self.assertEqual(len(self.calls("PATCH", "/v3/users/")), 3)   # the first attempt stopped after its set
        self.assertIn(out["credential"]["id"], self.ks.app_creds)

    def test_role_refused(self):
        self.clouds = self.manager_account()
        with self.assertRaises(S.Refusal) as cm:
            S.cluster_identity(cluster_req(self, roles=["member", "load-balancer_member"]))
        self.assertEqual(cm.exception.reason, "RoleRefused")
        self.assertTrue(cm.exception.message.startswith(
            "The site refused the role load-balancer_member on the project for user shc-oso-acme-c1-0a1b2: "
            "You are not authorized to perform the requested action: identity:create_grant."), cm.exception.message)

    def test_a_domain_manager_account_is_enough(self):
        self.clouds = self.manager_account()
        out = S.cluster_identity(cluster_req(self))
        self.assertIn(out["credential"]["id"], self.ks.app_creds)

    def test_a_name_longer_than_keystone_allows_is_refused(self):
        with self.assertRaises(S.Refusal) as cm:
            S.cluster_identity(cluster_req(self, user_name="u" * 256))
        self.assertEqual(cm.exception.reason, "UserRefused")
        self.assertIn("longer than the site's identity service allows (255 characters)", cm.exception.message)
        self.assertEqual(self.calls("POST", "/v3/users"), [])


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class TokenInTheSecondOfTheSet(Base):
    """N1: without the wait the user's token is refused at its first use. With the fake's clock held still every
    attempt meets that (deterministic), and the unit gives up after five attempts with a plain refusal."""
    settle = 0.0

    def test_without_the_wait_every_attempt_is_refused(self):
        self.ks.tick = lambda: 7
        with self.assertRaises(S.Refusal) as cm:
            S.cluster_identity(cluster_req(self))
        self.assertEqual(cm.exception.reason, "CredentialRefused")
        self.assertIn("after 5 attempts", cm.exception.message)
        self.assertEqual(len([c for c in self.ks.log if c[0] == "GET" and c[1].endswith("application_credentials")
                              and c[2] == 401]), 5)
        self.assertEqual(self.ks.app_creds, {})


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class ClusterIdentityDelete(Base):
    def test_the_users_credentials_go_with_it(self):
        out = S.cluster_identity(cluster_req(self))
        cid = out["credential"]["id"]
        cred = dict(self.ks.app_creds[cid])
        res = S.cluster_identity_delete({"clouds_yaml": self.clouds, "domain": "tenants", "user_id": out["user"]["id"],
                                         "user_name": "shc-oso-acme-c1-0a1b2"})
        self.assertEqual(res, {"found": True, "deleted": True, "id": out["user"]["id"]})
        k = S.Keystone(S.Account({"clouds_yaml": self.clouds}))
        self.assertEqual(k.app_cred_token(cid, cred["secret"])[0], 401)
        again = S.cluster_identity_delete({"clouds_yaml": self.clouds, "domain": "tenants", "user_id": out["user"]["id"],
                                           "user_name": "shc-oso-acme-c1-0a1b2"})
        self.assertEqual(again, {"found": False, "deleted": False, "id": ""})

    def test_by_name_and_never_a_foreign_user(self):
        foreign = self.ks.add_user("shc-x", self.tenants, description="someone's")
        res = S.cluster_identity_delete({"clouds_yaml": self.clouds, "domain": "tenants", "user_id": "",
                                         "user_name": "shc-x"})
        self.assertEqual((res["deleted"], res["kept"]), (False, "not the platform's"))
        self.assertIn(foreign, self.ks.users)

    def test_a_domain_manager_deletes_the_user(self):
        out = S.cluster_identity(cluster_req(self))
        mgr = self.manager_account()
        res = S.cluster_identity_delete({"clouds_yaml": mgr, "domain": "tenants", "user_id": out["user"]["id"]})
        self.assertTrue(res["deleted"])
        self.assertEqual([c for c in self.ks.app_creds.values() if c["user"] == out["user"]["id"]], [])


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class LeftoverUsers(Base):
    def test_only_the_platforms_users_with_a_role_on_the_project(self):
        mine = self.ks.add_user("shc-left", self.tenants, description="c. " + S.MARKER)
        self.ks.grant("user", mine, "project", self.pid, "member")
        theirs = self.ks.add_user("person", self.tenants, description="")
        self.ks.grant("user", theirs, "project", self.pid, "member")
        elsewhere = self.ks.add_user("shc-other", self.tenants, description="c. " + S.MARKER)
        other = self.ks.add_project("acme_env2", self.tenants)
        self.ks.grant("user", elsewhere, "project", other, "member")
        res = S.leftover_users({"clouds_yaml": self.clouds, "domain": "tenants", "project_id": self.pid})
        self.assertEqual(res, {"deleted": [{"id": mine, "name": "shc-left"}]})
        self.assertIn(theirs, self.ks.users)
        self.assertIn(elsewhere, self.ks.users)


def group_req(t, **over):
    req = {"action": "access-groups", "clouds_yaml": t.clouds, "state": "present", "domain": "tenants",
           "project_id": t.pid, "group_domain": "tenants", "entity": "acme", "managed_roles": ["member", "reader"],
           "groups": [{"group": "acme.developers", "roles": ["member"], "description": "Access group acme/developers"},
                      {"group": "acme.auditors", "roles": ["reader"], "description": "Access group acme/auditors"}],
           "keep": []}
    req.update(over)
    return req


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class AccessGroups(Base):
    def gid(self, name, dom=None):
        return next(g["id"] for g in self.ks.groups.values() if g["name"] == name
                    and g["domain_id"] == (dom or self.tenants))

    def roles_of(self, name, dom=None):
        gid = self.gid(name, dom)
        return {self.ks.roles[r]["name"] for (k, a, sk, sid, r) in self.ks.assignments
                if k == "group" and a == gid and sid == self.pid}

    def test_groups_made_marked_and_granted_and_a_second_run_changes_nothing(self):
        out = S.access_groups(group_req(self))
        self.assertEqual([(g["group"], g["state"], g["created"]) for g in out["groups"]],
                         [("acme.developers", "Granted", True), ("acme.auditors", "Granted", True)])
        self.assertEqual(out["groupDomainId"], self.tenants)
        self.assertEqual(self.roles_of("acme.developers"), {"member"})
        self.assertEqual(self.roles_of("acme.auditors"), {"reader"})
        self.assertIn(S.MARKER, self.ks.groups[self.gid("acme.developers")]["description"])
        self.assertEqual(out["unwanted"], [])
        n = len(self.ks.log)
        again = S.access_groups(group_req(self))
        self.assertEqual([g["created"] for g in again["groups"]], [False, False])
        self.assertEqual([c for c in self.ks.log[n:] if c[0] in ("POST", "PUT", "PATCH", "DELETE")
                          and c[1] != "/v3/auth/tokens"], [])

    def test_a_foreign_group_of_that_name_fails_alone(self):
        self.ks.add_group("acme.developers", self.tenants, description="the operator's")
        out = S.access_groups(group_req(self))
        dev, aud = out["groups"]
        self.assertEqual(dev["state"], "Failed")
        self.assertEqual(dev["message"], "a group named acme.developers exists in domain tenants and is not the platform's")
        self.assertEqual(aud["state"], "Granted")
        self.assertEqual(self.roles_of("acme.developers"), set())

    def test_an_access_group_moved_from_edit_to_view(self):
        S.access_groups(group_req(self))
        S.access_groups(group_req(self, groups=[{"group": "acme.developers", "roles": ["reader"]}],
                                  keep=["acme.auditors"]))
        self.assertEqual(self.roles_of("acme.developers"), {"reader"})
        self.assertEqual(self.roles_of("acme.auditors"), {"reader"})

    def test_unwanted_groups_are_reported_not_touched(self):
        S.access_groups(group_req(self))
        out = S.access_groups(group_req(self, groups=[{"group": "acme.developers", "roles": ["member"]}]))
        self.assertEqual(out["unwanted"], ["acme.auditors"])
        self.assertEqual(self.roles_of("acme.auditors"), {"reader"})
        # a group kept while it waits for its directory group is not unwanted
        out = S.access_groups(group_req(self, groups=[{"group": "acme.developers", "roles": ["member"]}],
                                        keep=["acme.auditors"]))
        self.assertEqual(out["unwanted"], [])

    def test_absent_revokes_and_deletes_a_group_that_holds_nothing_else(self):
        S.access_groups(group_req(self))
        other = self.ks.add_project("acme_env2", self.tenants)
        self.ks.grant("group", self.gid("acme.auditors"), "project", other, "reader")
        out = S.access_groups(group_req(self, state="absent", groups=[{"group": "acme.developers", "delete": True},
                                                                      {"group": "acme.auditors", "delete": True},
                                                                      {"group": "acme.gone", "delete": True}]))
        dev, aud, gone = out["groups"]
        self.assertEqual((dev["revoked"], dev["deleted"]), (["member"], True))
        self.assertEqual((aud["revoked"], aud["deleted"], aud["kept"]), (["reader"], False, "holds a role on another project"))
        self.assertEqual(gone["kept"], "not found")
        self.assertNotIn("acme.developers", [g["name"] for g in self.ks.groups.values()])

    def test_absent_without_delete_keeps_the_group(self):
        S.access_groups(group_req(self))
        out = S.access_groups(group_req(self, state="absent", groups=[{"group": "acme.developers", "delete": False}]))
        self.assertEqual((out["groups"][0]["revoked"], out["groups"][0]["deleted"]), (["member"], False))
        self.assertTrue(self.gid("acme.developers"))

    def test_a_domain_manager_in_the_tenant_domain(self):
        self.clouds = self.manager_account()
        out = S.access_groups(group_req(self))
        self.assertEqual([g["state"] for g in out["groups"]], ["Granted", "Granted"])

    def test_groups_in_a_federation_domain_need_admin_for_the_grant(self):
        fed = self.ks.add_domain("federated")
        mgr_uid, mgr_pw, _ = self.ks.site_account(roles=("manager",), domain_scope=self.tenants, name="m2")
        self.ks.grant("user", mgr_uid, "domain", fed, "manager")
        self.clouds = self.ks.clouds_yaml(os.path.join(TMP, f"m2-{id(self)}.yaml"), "m2", mgr_pw,
                                          project="m2-project", domain="tenants")
        # a domain manager (here also manager of the federation domain) may not grant a group of another domain a
        # role on the project (A5): said plainly, before any call that would be refused
        with self.assertRaises(S.Refusal) as cm:
            S.access_groups(group_req(self, group_domain="federated"))
        self.assertEqual(cm.exception.reason, "GrantRefused")
        self.assertEqual(cm.exception.message, "The site account is not an administrator; sign-in groups in domain "
                                               "federated, which is not the project's domain, need the admin role")
        self.assertEqual([c for c in self.ks.log if c[0] in ("POST", "PUT") and c[1] != "/v3/auth/tokens"], [])
        # the cloud administrator may
        self.ks.grant("user", self.uid, "domain", fed, "admin")
        self.clouds = self.ks.clouds_yaml(os.path.join(TMP, f"acct2-{id(self)}.yaml"), "site-admin", self.pw,
                                          project="site-admin-project")
        out = S.access_groups(group_req(self, group_domain="federated"))
        self.assertEqual([g["state"] for g in out["groups"]], ["Granted", "Granted"])
        self.assertEqual(self.roles_of("acme.developers", fed), {"member"})


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class Provision(Base):
    def test_no_credential_is_made_and_the_legacy_one_goes(self):
        S.provision({"clouds_yaml": self.clouds, "domain": "tenants", "project": "acme_env1"})
        # the account's own credential of an earlier release (it owns it, scoped to the project)
        legacy = "legacy-cred"
        self.ks.app_creds[legacy] = {"id": legacy, "user": self.uid, "project": self.pid, "name": "sovereign-acme_env1",
                                     "description": "", "secret": "old-secret", "roles": ["member"],
                                     "unrestricted": False, "expires_at": None}
        self.ks.app_creds["by-name"] = dict(self.ks.app_creds[legacy], id="by-name")
        self.ks.app_creds["by-name"]["name"] = "sovereign-acme_env1"
        n = len(self.ks.app_creds)
        out = S.provision({"clouds_yaml": self.clouds, "domain": "tenants", "project": "acme_env1",
                           "legacy_app_cred_ids": [legacy], "legacy_app_cred_name": "sovereign-acme_env1"})
        self.assertEqual(out["project"]["id"], self.pid)
        self.assertEqual(sorted(out["legacy_app_creds_deleted"]), ["by-name", legacy])
        self.assertEqual(len(self.ks.app_creds), n - 2)
        self.assertNotIn("app_cred", out)
        again = S.provision({"clouds_yaml": self.clouds, "domain": "tenants", "project": "acme_env1",
                             "legacy_app_cred_ids": [legacy], "legacy_app_cred_name": "sovereign-acme_env1"})
        self.assertEqual(again["legacy_app_creds_deleted"], [legacy])   # recorded id: missing counts as deleted
        self.assertEqual([c for c in self.ks.log if c[0] == "POST" and "application_credentials" in c[1]], [])


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class SignInReads(Base):
    MAPPING = [{"local": [{"user": {"id": "{0}", "name": "{1}"}}],
                "remote": [{"type": "OIDC-sub"}, {"type": "OIDC-preferred_username"}]},
               {"local": [{"groups": "{0}", "domain": {"name": "tenants"}}], "remote": [{"type": "OIDC-groups"}]}]

    def declare(self, rules=None, ttl=1440):
        fed = self.ks.add_domain("federated")
        self.ks.idps["platform-tenants"] = {"id": "platform-tenants", "enabled": True, "domain_id": fed,
                                            "remote_ids": ["https://sso.example.org/realms/tenants"],
                                            "authorization_ttl": ttl}
        self.ks.protocols[("platform-tenants", "openid")] = {"id": "openid", "mapping_id": "m1"}
        self.ks.mappings["m1"] = {"id": "m1", "rules": rules if rules is not None else self.MAPPING}
        return fed

    def read(self, clouds=None, **si):
        # as site_probe does: an administrator reads with its own token, a domain manager with one scoped to the
        # project domain
        acct = S.Account({"clouds_yaml": clouds or self.clouds})
        conn = acct.own_scope()
        access = conn.session.auth.get_access(conn.session)
        admin = "admin" in access.role_names
        notes = []
        ks = S.Keystone(acct)
        tok = S.identity_token(ks, conn.session.get_token(), admin, "tenants", notes)
        out = S.sign_in_reads(ks, tok, access.user_id,
                              dict({"identity_provider": "platform-tenants", "protocol": "openid",
                                    "group_domain": "tenants", "project_domain": "tenants", "admin": admin}, **si),
                              notes)
        return out, notes

    def test_an_administrator_reads_the_setup(self):
        fed = self.declare()
        out, notes = self.read()
        self.assertEqual(out["identityProvider"]["found"], True)
        self.assertEqual(out["identityProvider"]["authorizationTTL"], 1440)
        self.assertEqual(out["protocol"], {"read": True, "found": True, "mappingId": "m1"})
        self.assertEqual(out["mapping"]["warnings"], [])
        self.assertEqual(out["groupDomain"], {"name": "tenants", "read": True, "found": True, "id": self.tenants})
        self.assertEqual(out["userDomain"], {"id": fed, "name": "federated"})
        self.assertIn({"role": "admin", "scope": "domain tenants"}, out["account"]["roles"])
        self.assertEqual(notes, [])
        self.assertEqual([c for c in self.ks.log if c[0] != "GET" and c[1] != "/v3/auth/tokens"], [])

    def test_mapping_warnings(self):
        self.declare(rules=[{"local": [{"user": {"name": "{0}"}}, {"group": {"name": "{1}", "domain": {"name": "x"}}},
                                       {"projects": [{"name": "p", "roles": [{"name": "member"}]}]}],
                             "remote": [{"type": "OIDC-preferred_username"}, {"type": "OIDC-groups"}]}])
        out, _ = self.read()
        self.assertEqual(out["mapping"]["warnings"],
                         ["uses a project list", "does not look groups up in tenants", "has no stable user id"])

    def test_unset_ttl_is_not_zero(self):
        self.declare(ttl=None)
        out, _ = self.read()
        self.assertIsNone(out["identityProvider"]["authorizationTTL"])

    def test_a_domain_manager_may_not_read_the_federation_settings(self):
        self.declare()
        out, _ = self.read(clouds=self.manager_account())
        self.assertEqual((out["identityProvider"]["read"], out["identityProvider"]["status"]), (False, 403))
        self.assertFalse(out["mapping"]["read"])
        self.assertTrue(out["groupDomain"]["found"])

    def test_the_start_url(self):
        self.declare()
        out, _ = self.read(start="IdentityProvider", dashboard_url="https://dashboard.site-a.example.org/")
        self.assertEqual(out["startUrl"]["url"],
                         self.ks.url + "/v3/auth/OS-FEDERATION/identity_providers/platform-tenants/protocols/openid/"
                         "websso?origin=https%3A%2F%2Fdashboard.site-a.example.org%2Fauth%2Fwebsso%2F")
        self.assertEqual((out["startUrl"]["checked"], out["startUrl"]["redirects"]), (True, True))
        out, _ = self.read(start="Dashboard", dashboard_url="https://dashboard.site-a.example.org")
        self.assertEqual(out["startUrl"], {"url": "https://dashboard.site-a.example.org", "checked": False,
                                           "redirects": False})

    def test_a_provider_that_does_not_exist(self):
        out, _ = self.read(identity_provider="nobody")
        self.assertEqual((out["identityProvider"]["read"], out["identityProvider"]["found"]), (True, False))


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class ProbeGrantsAdminOnlyToAnAdmin(unittest.TestCase):
    """The probe gives the account admin on the tenant domain only when it holds admin already; a domain manager's
    roles are reported, never widened (the Neutron and catalog parts are not this test's)."""

    def probe(self, roles, has_admin_there=False):
        conn = mock.MagicMock()
        access = mock.Mock(user_id="u1", project_id="p1", role_names=roles)
        access.service_catalog.catalog = [{"type": "identity"}]
        conn.session.auth.get_access.return_value = access
        conn.network.extensions.return_value = []
        conn.network.rbac_policies.return_value = []
        conn.identity.find_domain.return_value = mock.Mock(id="d1", is_enabled=True)
        conn.identity.validate_user_has_domain_role.return_value = has_admin_there
        acct = mock.Mock(own_auth={"auth_url": "https://keystone.example.org"}, region="r1")
        acct.own_scope.return_value = conn
        with mock.patch.object(S, "Account", return_value=acct):
            out = S.site_probe({"action": "site-probe", "clouds_yaml": "/dev/null", "project_domain": "tenants"})
        return out, conn

    def test_admin(self):
        out, conn = self.probe(["admin", "member", "reader"])
        conn.identity.assign_domain_role_to_user.assert_called_once()
        self.assertTrue(out["project_domain"]["admin_granted"])

    def test_admin_already_there(self):
        out, conn = self.probe(["admin"], has_admin_there=True)
        conn.identity.assign_domain_role_to_user.assert_not_called()
        self.assertFalse(out["project_domain"]["admin_granted"])

    def test_domain_manager(self):
        out, conn = self.probe(["manager", "member", "reader"])
        conn.identity.assign_domain_role_to_user.assert_not_called()
        self.assertFalse(out["project_domain"]["admin_granted"])
        self.assertTrue(out["project_domain"]["found"])


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class NoSecretInOutput(Base):
    """The credential unit with known secrets: the helper's whole stdout (main(), as the job runs it), the result and
    a refusal carry none of them; only the private Vault write body does."""

    def run_main(self, req):
        buf = io.StringIO()
        rc = S.main(io.StringIO(json.dumps(req)), buf)
        return rc, buf.getvalue()

    def test_success_and_refusal(self):
        n0 = len(S._SECRETS)
        req = cluster_req(self)
        rc, text = self.run_main(req)
        self.assertEqual(rc, 0, text)
        secrets_now = S._SECRETS[n0:] + [c["secret"] for c in self.ks.app_creds.values()]
        self.assertGreaterEqual(len(secrets_now), 3)
        for s in secrets_now + [self.pw]:
            self.assertNotIn(s, text)
        with open(req["out_file"]) as f:
            written = f.read()
        self.assertTrue(any(s in written for s in secrets_now))
        # a refusal in the middle (the second password set refused, Keystone's message echoing the password in the
        # worst case): no secret in the helper's output
        uid = json.loads(text)["user"]["id"]
        patches = []

        def refuse_second(method, path):
            if method == "PATCH" and path.startswith(f"/v3/users/{uid}"):
                patches.append(path)
                if len(patches) == 2:
                    self.ks.fail_next.append(("PATCH", "/v3/users/", 403, "password rule: " + S._SECRETS[-1]))
        self.ks.on_request = refuse_second
        rc, text = self.run_main(cluster_req(self))
        self.assertEqual(rc, 1)
        body = json.loads(text)
        self.assertEqual(body["reason"], "PasswordRefused")
        for s in S._SECRETS[n0:] + [c["secret"] for c in self.ks.app_creds.values()] + [self.pw]:
            self.assertNotIn(s, text)
        self.assertIn("<secret>", text)   # Keystone's message echoed a password: replaced


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class KeepCheck(Base):
    """R1, R10: the credential in Vault is replaced only when the site says it is not valid (401, 404 at its sign-in)
    or it signs in as another user or project; any other answer stops the step with nothing changed."""

    def first(self):
        req = cluster_req(self)
        out = S.cluster_identity(req)
        return out, vault_read_of(req["out_file"], os.path.join(TMP, f"vault-{id(self)}-{len(self.ks.log)}.json"))

    def test_a_5xx_or_no_answer_changes_nothing(self):
        out, vf = self.first()
        for status in (500, 503, 403, 429, 400, 408):
            n = len(self.ks.log)
            # the site account's password sign-ins pass, the credential's sign-in is answered with the status once
            orig = self.ks._auth

            def auth(body, status=status, orig=orig):
                if "application_credential" in (((body.get("auth") or {}).get("identity") or {}).get("methods") or []):
                    self.ks._auth = orig
                    return self.ks._err(status, "Service Unavailable")
                return orig(body)
            self.ks._auth = auth
            with self.assertRaises(S.NotAnswered) as cm:
                S.cluster_identity(cluster_req(self, vault_file=vf))
            self.assertIn(f"(HTTP {status}); nothing was changed", cm.exception.message)
            later = self.ks.log[n:]
            self.assertEqual([c for c in later if c[0] in ("PATCH", "DELETE")
                              or (c[0] == "POST" and "application_credentials" in c[1])], [])
            self.assertIn(out["credential"]["id"], self.ks.app_creds)
        # no connection at all
        with mock.patch.object(S.Keystone, "app_cred_token", side_effect=__import__("requests").ConnectionError("x")):
            with self.assertRaises(S.NotAnswered):
                S.cluster_identity(cluster_req(self, vault_file=vf))
        self.assertIn(out["credential"]["id"], self.ks.app_creds)
        # the helper's exit for it: an error marked transient, no refusal reason
        orig = self.ks._auth

        def auth(body):
            if "application_credential" in (((body.get("auth") or {}).get("identity") or {}).get("methods") or []):
                self.ks._auth = orig
                return self.ks._err(503, "x")
            return orig(body)
        self.ks._auth = auth
        buf = io.StringIO()
        self.assertEqual(S.main(io.StringIO(json.dumps(cluster_req(self, vault_file=vf))), buf), 1)
        body = json.loads(buf.getvalue())
        self.assertTrue(body["transient"])
        self.assertNotIn("reason", body)

    def test_a_credential_of_another_user_or_project_is_replaced(self):
        out, vf = self.first()
        other_pid = self.ks.add_project("acme_env9", self.tenants)
        # the credential Vault holds now signs in, but as another project: not this cluster's
        self.ks.app_creds[out["credential"]["id"]]["project"] = other_pid
        self.ks.grant("user", out["user"]["id"], "project", other_pid, "member")
        again = S.cluster_identity(cluster_req(self, vault_file=vf))
        self.assertFalse(again["credential"]["kept"])
        # and as another user
        out2, vf2 = self.first()
        other = self.ks.add_user("someone", self.tenants, description=S.MARKER)
        self.ks.grant("user", other, "project", self.pid, "member")
        self.ks.app_creds[out2["credential"]["id"]]["user"] = other
        again = S.cluster_identity(cluster_req(self, vault_file=vf2))
        self.assertFalse(again["credential"]["kept"])

    def test_a_401_or_404_makes_a_new_one(self):
        out, vf = self.first()
        del self.ks.app_creds[out["credential"]["id"]]                  # 401 from the fake
        self.assertFalse(S.cluster_identity(cluster_req(self, vault_file=vf))["credential"]["kept"])

    def test_the_vault_file_may_be_the_clouds_yaml_itself(self):
        req = cluster_req(self)
        S.cluster_identity(req)
        with open(req["out_file"]) as f:
            text = json.load(f)["data"]["clouds.yaml"]
        path = os.path.join(TMP, f"clouds-{id(self)}.yaml")
        with open(path, "w") as f:
            f.write(text)
        self.assertTrue(S.cluster_identity(cluster_req(self, vault_file=path))["credential"]["kept"])

    def test_a_wait_after_the_second_password_set(self):
        """R14: the unit returns only PASSWORD_SETTLE_SECONDS after the second set (the next step signs in with the
        new credential; a token of the second of a reset is refused)."""
        sleeps = []
        real_sleep = S.time.sleep
        with mock.patch.object(S.time, "sleep", side_effect=lambda t: (sleeps.append(t), real_sleep(t))):
            S.cluster_identity(cluster_req(self))
        self.assertEqual(sleeps, [self.settle, self.settle])
        last_patch = max(i for i, c in enumerate(self.ks.log) if c[0] == "PATCH")
        self.assertEqual(self.ks.log[last_patch:][1:], [])


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class Guards(Base):
    """R9, R10, R16: a delete never runs with a filter an empty value widened, never outside the domain, never on a
    user of another name."""

    def test_empty_filters_are_refused(self):
        mine = self.ks.add_user("shc-left", self.tenants, description="c. " + S.MARKER)
        self.ks.grant("user", mine, "project", self.pid, "member")
        n = len(self.ks.log)
        for req in ({"domain": "tenants", "project_id": ""}, {"domain": "", "project_id": self.pid},
                    {"domain": "tenants"}):
            with self.assertRaises(ValueError):
                S.leftover_users(dict(req, clouds_yaml=self.clouds, action="leftover-users"))
        for req in ({"domain": "tenants", "user_id": "", "user_name": ""}, {"domain": "", "user_id": mine}):
            with self.assertRaises(ValueError):
                S.cluster_identity_delete(dict(req, clouds_yaml=self.clouds))
        for over in ({"project_id": ""}, {"domain": ""}, {"group_domain": ""}):
            for state in ("present", "absent"):
                with self.assertRaises(ValueError):
                    S.access_groups(group_req(self, state=state, **over))
        self.assertEqual(self.ks.log[n:], [])
        self.assertIn(mine, self.ks.users)

    def test_leftover_users_of_another_domain_stay(self):
        other_dom = self.ks.add_domain("elsewhere")
        stranger = self.ks.add_user("shc-x", other_dom, description="c. " + S.MARKER)
        self.ks.grant("user", stranger, "project", self.pid, "member")
        res = S.leftover_users({"clouds_yaml": self.clouds, "domain": "tenants", "project_id": self.pid})
        self.assertEqual(res, {"deleted": []})
        self.assertIn(stranger, self.ks.users)

    def test_delete_by_id_outside_the_domain_or_of_another_name(self):
        other_dom = self.ks.add_domain("elsewhere")
        stranger = self.ks.add_user("shc-x", other_dom, description="c. " + S.MARKER)
        res = S.cluster_identity_delete({"clouds_yaml": self.clouds, "domain": "tenants", "user_id": stranger})
        self.assertEqual((res["deleted"], res["kept"]), (False, "not the platform's"))
        self.assertIn(stranger, self.ks.users)
        sibling = self.ks.add_user("shc-oso-acme-c2-9z9z9", self.tenants, description="c. " + S.MARKER)
        res = S.cluster_identity_delete({"clouds_yaml": self.clouds, "domain": "tenants", "user_id": sibling,
                                         "user_name": "shc-oso-acme-c1-0a1b2"})
        self.assertFalse(res["deleted"])
        self.assertIn(sibling, self.ks.users)

    def test_unwanted_are_this_tenants_groups_only(self):
        S.access_groups(group_req(self))
        other = self.ks.add_group("globex.developers", self.tenants, description="x. " + S.MARKER)
        self.ks.grant("group", other, "project", self.pid, "member")
        out = S.access_groups(group_req(self))
        self.assertEqual(out["unwanted"], [])


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class GroupFaults(Base):
    """R5, R19, R20, spike B name limits: a site that does not answer for one group leaves it Unchecked (nothing is
    known, nothing is refused); a refusal is Failed with a plain message; a name over the limit is refused plainly."""

    def test_a_5xx_on_one_grant_is_unchecked_not_failed(self):
        S.access_groups(group_req(self))
        gid = next(g["id"] for g in self.ks.groups.values() if g["name"] == "acme.developers")
        self.ks.assignments = {a for a in self.ks.assignments if a[1] != gid}
        self.ks.fail_next.append(("PUT", f"/v3/projects/{self.pid}/groups/{gid}", 503, "Service Unavailable"))
        out = S.access_groups(group_req(self))
        dev, aud = out["groups"]
        self.assertEqual((dev["state"], dev["message"]), ("Unchecked", "Service Unavailable"))
        self.assertEqual(aud["state"], "Granted")

    def test_a_refusal_holds_what_it_held(self):
        S.access_groups(group_req(self))
        gid = next(g["id"] for g in self.ks.groups.values() if g["name"] == "acme.developers")
        self.ks.fail_next.append(("GET", "/v3/role_assignments", 403, "You are not authorized"))
        out = S.access_groups(group_req(self))
        self.assertEqual((out["groups"][0]["state"], out["groups"][0]["held"]), ("Failed", False))
        out = S.access_groups(group_req(self))
        self.assertEqual((out["groups"][0]["state"], out["groups"][0]["held"]), ("Granted", True))
        self.assertTrue(gid)

    def test_a_group_name_over_the_limit(self):
        long = "acme." + "r" * 60
        out = S.access_groups(group_req(self, groups=[{"group": long, "roles": ["member"]}]))
        self.assertEqual(out["groups"][0]["state"], "Failed")
        self.assertEqual(out["groups"][0]["message"],
                         f"the group name {long} is longer than the site allows (64 characters)")
        self.assertEqual(self.calls("POST", "/v3/groups"), [])
        # the fake refuses it as Keystone does (spike B)
        k = S.Keystone(S.Account({"clouds_yaml": self.clouds}))
        tok, _, dom = k.site("tenants")
        self.assertEqual(k.request(tok, "POST", "/groups", {"group": {"name": long, "domain_id": dom}})[0], 400)

    def test_removing_a_group_name_over_the_limit_finishes(self):
        """Gate part 2 G-1: Keystone 2026.2 answers a lookup by an over-long name with HTTP 500 and a page. Such a name
        cannot exist on the site: its removal is "not found" at once, without a call, and the other groups go on."""
        long = "acme." + "r" * 60
        S.access_groups(group_req(self))
        out = S.access_groups(group_req(self, state="absent", groups=[{"group": long, "delete": True},
                                                                       {"group": "acme.developers", "delete": True}]))
        rec = {g["group"]: g for g in out["groups"]}
        self.assertEqual((rec[long]["kept"], rec[long]["error"]), ("not found", ""))
        self.assertTrue(rec["acme.developers"]["deleted"])
        self.assertEqual(self.ks.long_name_lookups, [])
        # the fake answers such a lookup as Keystone does
        k = S.Keystone(S.Account({"clouds_yaml": self.clouds}))
        tok, _, dom = k.site("tenants")
        status, data, _ = k.request(tok, "GET", "/groups?" + urllib.parse.urlencode({"name": long, "domain_id": dom}))
        self.assertEqual((status, data), (500, {}))

    def test_an_answer_without_json_is_plain(self):
        """A failure answered with a page (no JSON): its status in plain words, never "None"."""
        k = S.Keystone(S.Account({"clouds_yaml": self.clouds}))
        tok, _, dom = k.site("tenants")
        with self.assertRaises(S.KsError) as cm:
            k.call(tok, "GET", "/groups?" + urllib.parse.urlencode({"name": "x" * 70, "domain_id": dom}))
        self.assertEqual(cm.exception.message, "the site answered HTTP 500")
        self.assertEqual(S._plain(cm.exception), "the site answered HTTP 500")

    def test_a_missing_managed_role_fails_only_the_group_that_needs_it(self):
        groups = [{"group": "acme.developers", "roles": ["member"]}, {"group": "acme.auditors", "roles": ["observer"]}]
        out = S.access_groups(group_req(self, managed_roles=["member", "observer"], groups=groups))
        dev, aud = out["groups"]
        self.assertEqual(dev["state"], "Granted")
        self.assertEqual((aud["state"], aud["message"]), ("Failed", "role observer does not exist on the site"))

    def test_a_refusal_without_keystones_message_is_plain(self):
        self.ks.fail_next.append(("POST", "/v3/groups", 403, ""))
        out = S.access_groups(group_req(self))
        self.assertEqual(out["groups"][0]["message"], "the site answered HTTP 403")


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class GroupDeleteTrust(Base):
    """R8(b): a group is deleted only when the read of its roles elsewhere can be trusted to be complete."""

    def fed_admin(self):
        fed = self.ks.add_domain("federated")
        self.ks.grant("user", self.uid, "domain", fed, "admin")
        return fed

    def test_a_read_that_is_refused_keeps_the_group(self):
        S.access_groups(group_req(self))
        # the read of the group's roles elsewhere is refused (the revoke's own read, scoped to the project, passes)
        orig = self.ks._role_assignments

        def ra(q, tok):
            if "group.id" in q and "scope.project.id" not in q:
                return self.ks._err(403, "You are not authorized")
            return orig(q, tok)
        self.ks._role_assignments = ra
        out = S.access_groups(group_req(self, state="absent", groups=[{"group": "acme.developers", "delete": True}]))
        g = out["groups"][0]
        self.assertEqual((g["revoked"], g["deleted"]), (["member"], False))
        self.assertEqual(g["kept"], "its roles on other projects could not be read (HTTP 403)")
        self.assertIn("acme.developers", [x["name"] for x in self.ks.groups.values()])

    def test_federation_domain_read_with_the_administrators_own_token(self):
        fed = self.fed_admin()
        S.access_groups(group_req(self, group_domain="federated"))
        gid = next(g["id"] for g in self.ks.groups.values() if g["name"] == "acme.auditors" and g["domain_id"] == fed)
        # the group holds a role on a project of the tenant domain too: a token scoped to the federation domain would
        # not list it (a domain-scoped list holds its own domain only); the administrator's own token does
        other = self.ks.add_project("acme_env2", self.tenants)
        self.ks.grant("group", gid, "project", other, "reader")
        out = S.access_groups(group_req(self, state="absent", group_domain="federated",
                                        groups=[{"group": "acme.auditors", "delete": True}]))
        self.assertEqual(out["groups"][0]["kept"], "holds a role on another project")
        self.assertIn(gid, self.ks.groups)

    def test_an_administrator_without_a_role_on_the_group_domain_is_told_plainly(self):
        """R8(a): the account cannot get a token for the federation domain: a plain refusal naming the domain."""
        self.ks.add_domain("federated")
        with self.assertRaises(S.Refusal) as cm:
            S.access_groups(group_req(self, group_domain="federated"))
        self.assertEqual(cm.exception.reason, "GrantRefused")
        self.assertEqual(cm.exception.message, "The site account holds no role on domain federated; sign-in groups in "
                                               "a domain other than the project's need the admin role there")

    def test_the_fake_filters_a_managers_list_to_its_domain(self):
        """spike B: a manager's domain-scoped /role_assignments holds its own domain's; a project-scoped one is
        refused; /domains?name=<another domain> is an empty list."""
        mgr = self.manager_account()
        fed = self.ks.add_domain("federated")
        S.access_groups(group_req(self))
        gid = next(g["id"] for g in self.ks.groups.values() if g["name"] == "acme.auditors")
        self.ks.grant("group", gid, "domain", fed, "reader")
        k = S.Keystone(S.Account({"clouds_yaml": mgr}))
        tok, _, _ = k.site("tenants")
        st, body, _ = k.request(tok, "GET", f"/role_assignments?group.id={gid}")
        self.assertEqual((st, len(body["role_assignments"])), (200, 1))
        st, body, _ = k.request(tok, "GET", "/domains?name=federated")
        self.assertEqual((st, body["domains"]), (200, []))
        acct = S.Account({"clouds_yaml": mgr})
        ptok = acct.own_scope().session.get_token()
        for path in ("/roles?name=member", "/domains?name=tenants", f"/role_assignments?group.id={gid}",
                     f"/users?domain_id={self.tenants}", f"/groups?domain_id={self.tenants}"):
            self.assertEqual(k.request(ptok, "GET", path)[0], 403, path)


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class SignInReadFaults(SignInReads):
    """R6: only 404 (an empty list for the group domain) means "does not exist"; a 5xx is not a result."""

    def test_a_5xx_is_unreadable_not_missing(self):
        self.declare()
        self.ks.fail_next.append(("GET", "/v3/OS-FEDERATION/identity_providers/platform-tenants", 503, "x"))
        out, _ = self.read()
        self.assertEqual((out["identityProvider"]["read"], out["identityProvider"]["found"]), (False, False))
        self.assertEqual(out["unreadable"], ["identityProvider (HTTP 503)"])
        self.ks.fail_next.append(("GET", "/v3/domains", 500, "x"))
        out, _ = self.read()
        self.assertEqual((out["groupDomain"]["read"], out["groupDomain"]["found"]), (False, False))
        self.assertEqual(out["unreadable"], ["groupDomain (HTTP 500)"])

    def test_a_404_is_missing(self):
        out, _ = self.read(identity_provider="nobody")
        self.assertEqual(out["unreadable"], [])
        out, _ = self.read(group_domain="nowhere")
        self.assertEqual((out["groupDomain"]["read"], out["groupDomain"]["found"]), (True, False))

    def test_a_managers_reads_use_a_domain_token(self):
        self.declare()
        out, notes = self.read(clouds=self.manager_account())
        self.assertTrue(out["groupDomain"]["found"])
        self.assertTrue(out["account"]["read"])
        self.assertIn({"role": "manager", "scope": "domain tenants"}, out["account"]["roles"])

    def test_another_group_domain_needs_an_administrator(self):
        self.declare()
        out, notes = self.read(clouds=self.manager_account(), group_domain="federated")
        self.assertEqual((out["groupDomain"]["read"], out["groupDomain"]["found"]), (False, False))
        self.assertIn("only an administrator account can find it", " ".join(notes))



@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class SecondPass(Base):
    """V1: a recorded credential whose secret is not in Vault is never replaced by the job. V3: an answer that is not a
    refusal is "the site did not answer", never UserRefused / RoleRefused / PasswordRefused. V6: the probe's DNS
    project lookup works for a domain manager (identity reads with a domain-scoped token)."""

    def test_a_recorded_credential_without_its_secret_changes_nothing(self):
        out = S.cluster_identity(cluster_req(self))
        cid = out["credential"]["id"]
        garbage = os.path.join(TMP, f"garbage-{id(self)}.yaml")
        with open(garbage, "w") as f:
            f.write("not: [yaml")
        for vf in ("", garbage, os.path.join(TMP, "does-not-exist.yaml")):
            n = len(self.ks.log)
            with self.assertRaises(S.Refusal) as cm:
                S.cluster_identity(cluster_req(self, vault_file=vf, recorded_credential_id=cid))
            self.assertEqual(cm.exception.reason, "CredentialSecretMissing")
            self.assertIn("not in the platform's secret store", cm.exception.message)
            self.assertIn("nothing was changed", cm.exception.message)
            self.assertEqual(self.ks.log[n:], [])
            self.assertIn(cid, self.ks.app_creds)
        # nothing recorded: a new credential is made (no cluster can hold one)
        again = S.cluster_identity(cluster_req(self, vault_file="", recorded_credential_id=""))
        self.assertFalse(again["credential"]["kept"])

    def test_a_5xx_outside_the_keep_check_is_not_a_refusal(self):
        cases = [("GET", "/v3/users", "the cluster's user"), ("PUT", f"/v3/projects/{self.pid}/users", "the role"),
                 ("PATCH", "/v3/users/", "the password")]
        for method, prefix, step in cases:
            self.ks.fail_next.append((method, prefix, 503, "Service Unavailable"))
            with self.assertRaises(S.NotAnswered) as cm:
                S.cluster_identity(cluster_req(self, user_name=f"shc-oso-acme-c{len(self.ks.log)}-x",
                                               credential_name="c"))
            self.assertIn("did not answer " + step, cm.exception.message)
            self.assertNotIn("cannot be built", cm.exception.message)
            self.ks.fail_next.clear()
        # the exit: transient, no reason
        self.ks.fail_next.append(("GET", "/v3/users", 503, "Service Unavailable"))
        buf = io.StringIO()
        self.assertEqual(S.main(io.StringIO(json.dumps(cluster_req(self))), buf), 1)
        body = json.loads(buf.getvalue())
        self.assertTrue(body["transient"])
        self.assertNotIn("reason", body)

    def test_the_dns_project_lookup_of_a_domain_manager(self):
        mgr = self.manager_account()
        dns = self.ks.add_project("platform-dns", self.tenants)
        acct = S.Account({"clouds_yaml": mgr})
        conn = acct.own_scope()
        access = conn.session.auth.get_access(conn.session)
        notes = []
        out = S.probe_dns_project(acct, conn, access.user_id, False, {"domain": "tenants", "name": "platform-dns"}, notes)
        self.assertEqual((out["found"], out["id"], out["member_granted"]), (True, dns, True))
        self.assertEqual(notes, [])



@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class RecordedAgainstVault(Base):
    """W1: the credential in Vault is compared with the recorded one. (a) the same id: 201 keep, 401/404 replace,
    anything else nothing changed; (b) another id: kept only when it signs in as this cluster's user on this project,
    else CredentialSecretMissing with nothing changed; (c) nothing recorded: as before."""

    def first(self):
        req = cluster_req(self)
        out = S.cluster_identity(req)
        return out, out["credential"]["id"], out["user"]["id"]

    def vault_with(self, cid, secret):
        path = os.path.join(TMP, f"v-{id(self)}-{cid}.yaml")
        import yaml
        with open(path, "w") as f:
            yaml.safe_dump({"clouds": {"openstack": {"auth": {"application_credential_id": cid,
                                                               "application_credential_secret": secret}}}}, f)
        return path

    def extra_cred(self, uid, pid=None, cid="other-cred"):
        self.ks.app_creds[cid] = {"id": cid, "user": uid, "project": pid or self.pid, "name": "x", "description": "",
                                  "secret": "secret-" + cid, "roles": ["member"], "unrestricted": False,
                                  "expires_at": None}
        return cid, "secret-" + cid

    def writes(self, n):
        return [c for c in self.ks.log[n:] if c[0] in ("PATCH", "PUT", "DELETE")
                or (c[0] == "POST" and c[1] != "/v3/auth/tokens")]

    def test_a_same_id_401_is_replaced_and_a_5xx_changes_nothing(self):
        _, x, uid = self.first()
        secret = self.ks.app_creds[x]["secret"]
        vf = self.vault_with(x, secret)
        orig = self.ks._auth

        def auth(body):
            if "application_credential" in (((body.get("auth") or {}).get("identity") or {}).get("methods") or []):
                self.ks._auth = orig
                return self.ks._err(503, "x")
            return orig(body)
        self.ks._auth = auth
        n = len(self.ks.log)
        with self.assertRaises(S.NotAnswered):
            S.cluster_identity(cluster_req(self, vault_file=vf, recorded_credential_id=x))
        self.assertEqual(self.writes(n), [])
        # the site says the recorded credential is not valid: made again
        del self.ks.app_creds[x]
        again = S.cluster_identity(cluster_req(self, vault_file=vf, recorded_credential_id=x))
        self.assertFalse(again["credential"]["kept"])

    def test_another_id_that_signs_in_as_this_cluster_is_kept(self):
        """A raced first write (or a run whose record failed): Vault holds Y, the record names X, Y is this
        cluster's: kept, its id returned (the role records it), X untouched, no password set."""
        _, x, uid = self.first()
        y, ysecret = self.extra_cred(uid)
        n = len(self.ks.log)
        out = S.cluster_identity(cluster_req(self, vault_file=self.vault_with(y, ysecret), recorded_credential_id=x))
        self.assertEqual((out["credential"]["id"], out["credential"]["kept"]), (y, True))
        self.assertEqual(self.writes(n), [])
        self.assertIn(x, self.ks.app_creds)

    def test_another_id_the_site_refuses_changes_nothing(self):
        """A rolled-back Vault version: Vault holds Z (gone from the site), the record names X the cluster runs on:
        CredentialSecretMissing, X stays, no password set."""
        _, x, uid = self.first()
        z, zsecret = self.extra_cred(uid, cid="old-z")
        del self.ks.app_creds[z]
        n = len(self.ks.log)
        with self.assertRaises(S.Refusal) as cm:
            S.cluster_identity(cluster_req(self, vault_file=self.vault_with(z, zsecret), recorded_credential_id=x))
        self.assertEqual(cm.exception.reason, "CredentialSecretMissing")
        self.assertIn("holds another credential (old-z)", cm.exception.message)
        self.assertEqual(self.writes(n), [])
        self.assertIn(x, self.ks.app_creds)

    def test_another_id_of_another_user_or_project_changes_nothing(self):
        _, x, uid = self.first()
        stranger = self.ks.add_user("someone", self.tenants, description=S.MARKER)
        self.ks.grant("user", stranger, "project", self.pid, "member")
        s_id, s_secret = self.extra_cred(stranger, cid="stranger-cred")
        other_pid = self.ks.add_project("acme_env9", self.tenants)
        self.ks.grant("user", uid, "project", other_pid, "member")
        o_id, o_secret = self.extra_cred(uid, pid=other_pid, cid="other-project-cred")
        for cid, sec in ((s_id, s_secret), (o_id, o_secret)):
            n = len(self.ks.log)
            with self.assertRaises(S.Refusal) as cm:
                S.cluster_identity(cluster_req(self, vault_file=self.vault_with(cid, sec), recorded_credential_id=x))
            self.assertEqual(cm.exception.reason, "CredentialSecretMissing")
            self.assertEqual(self.writes(n), [])
            self.assertIn(x, self.ks.app_creds)

    def test_another_id_with_the_site_not_answering_changes_nothing(self):
        _, x, uid = self.first()
        y, ysecret = self.extra_cred(uid)
        orig = self.ks._auth

        def auth(body):
            if "application_credential" in (((body.get("auth") or {}).get("identity") or {}).get("methods") or []):
                self.ks._auth = orig
                return self.ks._err(503, "x")
            return orig(body)
        self.ks._auth = auth
        n = len(self.ks.log)
        with self.assertRaises(S.NotAnswered):
            S.cluster_identity(cluster_req(self, vault_file=self.vault_with(y, ysecret), recorded_credential_id=x))
        self.assertEqual(self.writes(n), [])


if __name__ == "__main__":
    unittest.main()
