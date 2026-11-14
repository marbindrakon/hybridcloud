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
           "user_name": "shc-shc-oso-acme-c1-0a1b2", "description": "User of OpenShift cluster entity-acme/c1",
           "roles": ["member"], "credential_name": "shc-shc-oso-acme-c1-0a1b2",
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
        foreign = self.ks.add_user("shc-shc-oso-acme-c1-0a1b2", self.tenants, password="theirs", description="mine")
        with self.assertRaises(S.Refusal) as cm:
            S.cluster_identity(cluster_req(self))
        self.assertEqual(cm.exception.reason, "UserRefused")
        self.assertEqual(cm.exception.message,
                         "The site refused the cluster's user shc-shc-oso-acme-c1-0a1b2: a user named "
                         "shc-shc-oso-acme-c1-0a1b2 exists in domain tenants and is not the platform's. "
                         "The cluster cannot be built.")
        self.assertEqual(self.ks.users[foreign]["password"], "theirs")
        self.assertEqual(self.calls("PATCH") + self.calls("PUT"), [])

    def test_an_existing_user_gets_the_options_back(self):
        uid = self.ks.add_user("shc-shc-oso-acme-c1-0a1b2", self.tenants, description="x. " + S.MARKER,
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
                    if u["name"].startswith("shc-shc-oso"):
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
            "The site refused the role load-balancer_member on the project for user shc-shc-oso-acme-c1-0a1b2: "
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
                                         "user_name": "shc-shc-oso-acme-c1-0a1b2"})
        self.assertEqual(res, {"found": True, "deleted": True, "id": out["user"]["id"]})
        k = S.Keystone(S.Account({"clouds_yaml": self.clouds}))
        self.assertEqual(k.app_cred_token(cid, cred["secret"])[0], 401)
        again = S.cluster_identity_delete({"clouds_yaml": self.clouds, "domain": "tenants", "user_id": out["user"]["id"],
                                           "user_name": "shc-shc-oso-acme-c1-0a1b2"})
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
        out = S.access_groups(group_req(self, group_domain="federated"))
        self.assertEqual([g["state"] for g in out["groups"]], ["Failed", "Failed"])
        self.assertEqual(out["groups"][0]["message"],
                         "You are not authorized to perform the requested action: identity:create_grant.")
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
                                     "unrestricted": False, "expires_at": None, "created_at": ""}
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
        acct = S.Account({"clouds_yaml": clouds or self.clouds})
        conn = acct.own_scope()
        tok = conn.session.get_token()
        notes = []
        out = S.sign_in_reads(S.Keystone(acct), tok, conn.session.auth.get_access(conn.session).user_id,
                              dict({"identity_provider": "platform-tenants", "protocol": "openid",
                                    "group_domain": "tenants"}, **si), notes)
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


if __name__ == "__main__":
    unittest.main()
