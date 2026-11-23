#!/usr/bin/env python3
"""Proves that a Keycloak can write to its database, run as a CronJob.

A dedicated, ordinary user of a realm signs in through a dedicated client (the password grant), reads its own
account through the Account API, writes one of its own profile fields (by default its last name) with the current
time, reads it back, and signs out. The update is a database write of that user only and needs only the user's
own manage-account role: no administration right. (A service account cannot do this: the Account API refuses
service accounts.) A Keycloak whose database connections went read-only answers the write with an error while
its health endpoints still pass.

Exit 0: the write was taken and reads back. Exit 1: the Keycloak refused or lost the write. Exit 2: the check could
not sign in or the Keycloak did not answer. The credential (files username, password and, for a confidential
client, clientSecret in credentialDir) and the tokens are never logged.

Configuration: a JSON file (--config) with url, realm, clientId, and optionally attribute (lastName, firstName or
the name of an attribute the realm's user profile lets the user edit), credentialDir, caFile, timeoutSeconds.
Standard library only.

usage: identity_write_probe.py [--config FILE]
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULTS = {"url": "", "realm": "", "clientId": "", "attribute": "lastName",
            "credentialDir": "/etc/identity-credential", "caFile": "", "timeoutSeconds": 20}
STANDARD = ("firstName", "lastName")


class Failure(Exception):
    def __init__(self, code, what):
        super().__init__(what)
        self.code = code


def reason(raw):
    """A short, credential-free reason from a Keycloak error body (error, error_description, errorMessage)."""
    try:
        doc = json.loads(raw or b"")
    except ValueError:
        return ""
    if isinstance(doc, list):
        doc = doc[0] if doc and isinstance(doc[0], dict) else {}
    if not isinstance(doc, dict):
        return ""
    parts = [str(doc[k])[:160] for k in ("error", "error_description", "errorMessage", "field") if doc.get(k)]
    return (" (%s)" % "; ".join(parts)) if parts else ""


def with_value(rep, attribute, value):
    """The account representation with one field set; the read-only parts the Account API returns are dropped."""
    rep = {k: v for k, v in (rep or {}).items() if k != "userProfileMetadata"}
    if attribute in STANDARD:
        rep[attribute] = value
    else:
        attrs = dict(rep.get("attributes") or {})
        attrs[attribute] = [value]
        rep["attributes"] = attrs
    return rep


def value_of(rep, attribute):
    if attribute in STANDARD:
        return (rep or {}).get(attribute)
    vals = ((rep or {}).get("attributes") or {}).get(attribute) or []
    return vals[0] if vals else None


class Keycloak:
    def __init__(self, cfg):
        self.base = cfg["url"].rstrip("/") + "/realms/" + urllib.parse.quote(cfg["realm"])
        self.timeout = cfg["timeoutSeconds"]
        self.ctx = ssl.create_default_context(cafile=cfg["caFile"] or None) if self.base.startswith("https") else None

    def call(self, method, path, form=None, body=None, token=None):
        headers = {"Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise Failure(2, "%s %s: no answer (%s)" % (method, path, getattr(e, "reason", e)))


def read_secret(directory, name, required=True):
    p = os.path.join(directory, name)
    if os.path.exists(p):
        with open(p) as f:
            value = f.read().strip()
        if value:
            return value
    if required:
        raise SystemExit("the credential in %s has no %s" % (directory, name))
    return ""


def probe(kc, cfg, creds, stamp, out=print):
    """One write check; raises Failure, returns the value written."""
    form = {"grant_type": "password", "client_id": cfg["clientId"], "username": creds["username"],
            "password": creds["password"], "scope": "openid"}
    if creds.get("clientSecret"):
        form["client_secret"] = creds["clientSecret"]
    code, raw = kc.call("POST", "/protocol/openid-connect/token", form=form)
    if code != 200:
        raise Failure(2, "sign-in answered %d%s" % (code, reason(raw)))
    try:
        tokens = json.loads(raw)
        access, refresh = tokens["access_token"], tokens.get("refresh_token")
    except (ValueError, KeyError, TypeError):
        raise Failure(2, "sign-in answered without an access token")
    try:
        code, raw = kc.call("GET", "/account/?userProfileMetadata=false", token=access)
        if code != 200:
            raise Failure(2, "reading the account answered %d%s" % (code, reason(raw)))
        value = "write check %s" % stamp
        code, raw = kc.call("POST", "/account/", body=with_value(json.loads(raw), cfg["attribute"], value),
                            token=access)
        if code not in (200, 204):
            raise Failure(1, "the write of %s was refused: %d%s" % (cfg["attribute"], code, reason(raw)))
        code, raw = kc.call("GET", "/account/?userProfileMetadata=false", token=access)
        if code != 200:
            raise Failure(1, "reading the account after the write answered %d%s" % (code, reason(raw)))
        if value_of(json.loads(raw), cfg["attribute"]) != value:
            raise Failure(1, "the write of %s was answered but does not read back" % cfg["attribute"])
        return value
    finally:
        if refresh:
            # end the session the sign-in made (also a write); a failure here is said, not judged
            logout = {"client_id": cfg["clientId"], "refresh_token": refresh}
            if creds.get("clientSecret"):
                logout["client_secret"] = creds["clientSecret"]
            try:
                code, raw = kc.call("POST", "/protocol/openid-connect/logout", form=logout)
                if code not in (200, 204):
                    out("note: the sign-out answered %d%s" % (code, reason(raw)))
            except Failure as e:
                out("note: %s" % e)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/identity-write-probe/config.json")
    args = ap.parse_args(argv)
    cfg = dict(DEFAULTS)
    with open(args.config) as f:
        cfg.update(json.load(f) or {})
    for key in ("url", "realm", "clientId"):
        if not cfg.get(key):
            raise SystemExit("setting %s is required" % key)
    d = cfg["credentialDir"]
    creds = {"username": read_secret(d, "username"), "password": read_secret(d, "password"),
             "clientSecret": read_secret(d, "clientSecret", required=False)}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    where = "realm %s at %s" % (cfg["realm"], cfg["url"])
    try:
        probe(Keycloak(cfg), cfg, creds, stamp)
    except Failure as e:
        msg = ("the Keycloak refuses a write (%s): %s" if e.code == 1 else
               "the write check could not run (%s): %s") % (where, e)
        print(msg)
        try:
            with open("/dev/termination-log", "w") as f:
                f.write(msg)
        except OSError:
            pass
        return e.code
    print("the Keycloak takes writes (%s)" % where)
    return 0


if __name__ == "__main__":
    sys.exit(main())
