#!/usr/bin/env python3
"""Proves that a Keycloak can write to its database, run as a CronJob.

In a dedicated realm (one no application trusts and nobody signs in to), a client's service account takes a token
with the client-credentials grant, reads its own user through the realm's Admin API, writes its own last name (or
`attribute`) with the current time, and reads it back. Its only right is `manage-users` of that realm's
`realm-management` client. The token makes no lasting session (a client-credentials grant without refresh tokens
keeps its session in memory only). A Keycloak whose database connections went read-only answers the write with an
error while its health endpoints still pass.

Exit 0: the write was taken and reads back. Exit 1: the Keycloak refused or lost the write. Exit 2: the check could
not get a token or the Keycloak did not answer. The client secret (file clientSecret in credentialDir) and the token
never appear in any output.

Configuration: a JSON file (--config) with url (https), realm, clientId, and optionally attribute (default lastName),
credentialDir, caFile, timeoutSeconds. Standard library only. Redirects are not followed.

usage: identity_write_probe.py [--config FILE]
"""
import argparse
import base64
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
STANDARD = ("firstName", "lastName", "email")
SECRETS = set()


def redact(text):
    text = str(text)
    for s in SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


class Failure(Exception):
    def __init__(self, code, what):
        super().__init__(redact(what))
        self.code = code


def reason(raw):
    """A short reason from a Keycloak error body (error, error_description, errorMessage)."""
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
    """The user representation with one field set (the read-only metadata the API returns left out)."""
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


def token_subject(token):
    """The `sub` claim of a JWT access token (its own user's id); not verified here: the Admin API does that."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("sub")
    except (IndexError, ValueError, AttributeError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect is answered as an error, never followed with the credential


def make_transport(ca_file=None):
    """(request, timeout) -> (status, body). Redirects are not followed."""
    handlers = [_NoRedirect()]
    if ca_file is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file or None)))
    opener = urllib.request.build_opener(*handlers)

    def send(req, timeout):
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()
    return send


class Keycloak:
    def __init__(self, cfg, transport=None):
        base = cfg["url"].rstrip("/")
        if transport is None:
            if not base.startswith("https://"):
                raise SystemExit("the URL %s must use https" % base)
            transport = make_transport(cfg["caFile"] or "")
        realm = urllib.parse.quote(cfg["realm"])
        self.realm_url = base + "/realms/" + realm
        self.admin_url = base + "/admin/realms/" + realm
        self.timeout = cfg["timeoutSeconds"]
        self.send = transport

    def call(self, method, url, form=None, body=None, token=None):
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
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            return self.send(req, self.timeout)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise Failure(2, "%s %s: no answer (%s)" % (method, urllib.parse.urlsplit(url).path,
                                                         getattr(e, "reason", e)))


def probe(kc, cfg, secret, stamp):
    """One write check; raises Failure, returns the value written."""
    code, raw = kc.call("POST", kc.realm_url + "/protocol/openid-connect/token",
                        form={"grant_type": "client_credentials", "client_id": cfg["clientId"],
                              "client_secret": secret})
    if code != 200:
        raise Failure(2, "the token request answered %d%s" % (code, reason(raw)))
    try:
        token = json.loads(raw)["access_token"]
    except (ValueError, KeyError, TypeError):
        raise Failure(2, "the token request answered without an access token")
    SECRETS.add(token)
    user_id = token_subject(token)
    if not user_id:
        raise Failure(2, "the access token names no user (sub)")
    user_url = "%s/users/%s" % (kc.admin_url, urllib.parse.quote(user_id))
    code, raw = kc.call("GET", user_url + "?userProfileMetadata=false", token=token)
    if code != 200:
        raise Failure(2, "reading its own user answered %d%s" % (code, reason(raw)))
    value = "write check %s" % stamp
    code, raw = kc.call("PUT", user_url, body=with_value(json.loads(raw), cfg["attribute"], value), token=token)
    if code not in (200, 204):
        raise Failure(1, "the write of %s was refused: %d%s" % (cfg["attribute"], code, reason(raw)))
    code, raw = kc.call("GET", user_url + "?userProfileMetadata=false", token=token)
    if code != 200:
        raise Failure(1, "reading its own user after the write answered %d%s" % (code, reason(raw)))
    if value_of(json.loads(raw), cfg["attribute"]) != value:
        raise Failure(1, "the write of %s was answered but does not read back" % cfg["attribute"])
    return value


def main(argv=None, transport=None):
    """transport: for tests only (a plain-http fake); the real run uses https."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/identity-write-probe/config.json")
    args = ap.parse_args(argv)
    cfg = dict(DEFAULTS)
    with open(args.config) as f:
        cfg.update(json.load(f) or {})
    for key in ("url", "realm", "clientId"):
        if not cfg.get(key):
            raise SystemExit("setting %s is required" % key)
    path = os.path.join(cfg["credentialDir"], "clientSecret")
    secret = ""
    if os.path.exists(path):
        with open(path) as f:
            secret = f.read().strip()
    if not secret:
        raise SystemExit("the credential in %s has no clientSecret" % cfg["credentialDir"])
    SECRETS.add(secret)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    where = "realm %s at %s" % (cfg["realm"], cfg["url"])
    try:
        probe(Keycloak(cfg, transport), cfg, secret, stamp)
    except Failure as e:
        msg = redact(("the Keycloak refuses a write (%s): %s" if e.code == 1 else
                      "the write check could not run (%s): %s") % (where, e))
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
