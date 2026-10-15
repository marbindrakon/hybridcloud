#!/usr/bin/env python3
"""shc (design/P8-PLAN.md §7): create or adopt an SHC member account in the AWS Organization and bootstrap its
automation role. Runs with the job's AWS web identity (the management account's automation role; boto3 reads
AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE). Idempotent; prints one JSON line.

  shc_aws_account.py --name NAME --email EMAIL --ou OU_ID --purpose PURPOSE
                     [--member-role shc-automation] [--member-policy POLICY_ARN] [--dry-run]

- An existing account (same name or email) is adopted only if it carries the tag shc-managed=true.
- A new account is created with tags shc-managed=true, shc-purpose, Name, then moved into the OU.
- Through OrganizationAccountAccessRole, the member account gets role <member-role>, trusting only the calling
  automation role with external ID shc-<account id>, with <member-policy> attached.
- The chain is checked: the automation role assumes <member-role> with the external ID.
"""
import argparse
import json
import sys
import time

import boto3
import botocore.exceptions

TAG = "shc-managed"


def fail(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}))
    sys.exit(1)


def find_account(org, name, email):
    for page in org.get_paginator("list_accounts").paginate():
        for a in page["Accounts"]:
            if a["Name"] == name or a["Email"].lower() == email.lower():
                return a
    return None


def tags_of(org, account_id):
    out = {}
    for page in org.get_paginator("list_tags_for_resource").paginate(ResourceId=account_id):
        out.update({t["Key"]: t["Value"] for t in page["Tags"]})
    return out


def create_account(org, name, email, purpose):
    r = org.create_account(Email=email, AccountName=name, RoleName="OrganizationAccountAccessRole",
                           IamUserAccessToBilling="DENY",
                           Tags=[{"Key": TAG, "Value": "true"}, {"Key": "shc-purpose", "Value": purpose},
                                 {"Key": "Name", "Value": name}])
    req = r["CreateAccountStatus"]["Id"]
    for _ in range(120):  # up to 30 min
        s = org.describe_create_account_status(CreateAccountRequestId=req)["CreateAccountStatus"]
        if s["State"] == "SUCCEEDED":
            return s["AccountId"]
        if s["State"] == "FAILED":
            fail("account creation failed", reason=s.get("FailureReason"))
        time.sleep(15)
    fail("account creation did not finish in 30 min", request=req)


def assume(sts, arn, session, external_id=None, tries=40):
    kw = {"RoleArn": arn, "RoleSessionName": session}
    if external_id:
        kw["ExternalId"] = external_id
    for i in range(tries):  # a new account's roles take a few minutes to become assumable
        try:
            c = sts.assume_role(**kw)["Credentials"]
            return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                                 aws_session_token=c["SessionToken"])
        except botocore.exceptions.ClientError as e:
            if i == tries - 1 or e.response["Error"]["Code"] not in ("AccessDenied", "AccessDeniedException"):
                raise
            time.sleep(15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--ou", required=True)
    ap.add_argument("--purpose", required=True)
    ap.add_argument("--member-role", default="shc-automation")
    ap.add_argument("--member-policy", default="arn:aws:iam::aws:policy/AdministratorAccess")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    sts = boto3.client("sts")
    me = sts.get_caller_identity()
    mgmt = me["Account"]
    # arn:aws:sts::<acct>:assumed-role/<role>/<session> -> arn:aws:iam::<acct>:role/<role>
    caller_role = "arn:aws:iam::%s:role/%s" % (mgmt, me["Arn"].split("/")[1])
    org = boto3.client("organizations")

    acct = find_account(org, a.name, a.email)
    created = False
    if acct:
        if acct["Status"] != "ACTIVE":
            fail("account exists but is not ACTIVE", account=acct["Id"], status=acct["Status"])
        if tags_of(org, acct["Id"]).get(TAG) != "true":
            fail("account exists but is not tagged %s=true; not adopting it" % TAG, account=acct["Id"])
        account_id = acct["Id"]
    elif a.dry_run:
        print(json.dumps({"ok": True, "dryRun": True, "would": "create", "name": a.name, "email": a.email,
                          "ou": a.ou, "caller": caller_role}))
        return
    else:
        account_id = create_account(org, a.name, a.email, a.purpose)
        created = True

    parent = org.list_parents(ChildId=account_id)["Parents"][0]["Id"]
    if a.dry_run:
        print(json.dumps({"ok": True, "dryRun": True, "account": account_id, "parent": parent,
                          "would": "move" if parent != a.ou else "keep", "caller": caller_role}))
        return
    if parent != a.ou:
        org.move_account(AccountId=account_id, SourceParentId=parent, DestinationParentId=a.ou)

    admin = assume(sts, "arn:aws:iam::%s:role/OrganizationAccountAccessRole" % account_id, "shc-bootstrap")
    iam = admin.client("iam")
    external_id = "shc-%s" % account_id
    trust = json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": caller_role}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"sts:ExternalId": external_id}}}]})
    try:
        iam.get_role(RoleName=a.member_role)
        iam.update_assume_role_policy(RoleName=a.member_role, PolicyDocument=trust)
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(RoleName=a.member_role, AssumeRolePolicyDocument=trust, MaxSessionDuration=3600,
                        Description="SHC platform automation (assumed by the management account's automation role)",
                        Tags=[{"Key": TAG, "Value": "true"}])
    attached = [p["PolicyArn"] for p in iam.list_attached_role_policies(RoleName=a.member_role)["AttachedPolicies"]]
    if a.member_policy not in attached:
        iam.attach_role_policy(RoleName=a.member_role, PolicyArn=a.member_policy)

    member = assume(sts, "arn:aws:iam::%s:role/%s" % (account_id, a.member_role), "shc-check", external_id)
    who = member.client("sts").get_caller_identity()["Arn"]
    print(json.dumps({"ok": True, "account": account_id, "name": a.name, "email": a.email, "ou": a.ou,
                      "created": created, "memberRole": "arn:aws:iam::%s:role/%s" % (account_id, a.member_role),
                      "externalId": external_id, "check": who}))


if __name__ == "__main__":
    main()
