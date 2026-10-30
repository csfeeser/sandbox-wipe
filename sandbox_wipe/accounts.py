"""Find sandbox accounts by NAME through AWS Organizations and build per-account SSO profiles."""
import configparser
import os
import re
import shutil
from dataclasses import dataclass

import boto3


@dataclass
class Target:
    profile: str
    name: str = ""
    account_id: str = ""


def config_path():
    return os.environ.get("AWS_CONFIG_FILE") or os.path.expanduser("~/.aws/config")


def sso_session_of(profile):
    cp = configparser.ConfigParser()
    cp.read(config_path())
    for section in (f"profile {profile}", profile):
        if cp.has_section(section) and cp.has_option(section, "sso_session"):
            return cp.get(section, "sso_session")
    return ""


def resolve(via, regex, role, outdir, say=print):
    """Return [Target] for ACTIVE accounts whose name matches `regex`, never the login/management account.

    Writes DIR/aws-config (a copy of your AWS config plus one profile per match) and points
    AWS_CONFIG_FILE at it. Your own ~/.aws/config is never modified.
    """
    session = boto3.Session(profile_name=via)
    sso_session = sso_session_of(via)
    if not sso_session:
        raise SystemExit(f"profile {via} has no sso_session; cannot derive per-account SSO profiles")
    login_acct = session.client("sts").get_caller_identity()["Account"]
    mgmt = ""
    org = session.client("organizations")
    try:
        mgmt = org.describe_organization()["Organization"]["MasterAccountId"]
    except Exception:
        pass
    accounts = []
    for page in org.get_paginator("list_accounts").paginate():
        accounts += [a for a in page["Accounts"] if a.get("Status") == "ACTIVE"]
    rx = re.compile(regex)
    os.makedirs(outdir, exist_ok=True)
    gen = os.path.join(outdir, "aws-config")
    src = config_path()
    if os.path.abspath(src) != os.path.abspath(gen):
        shutil.copyfile(src, gen)
    targets = []
    say(f"Accounts matching {regex}:")
    with open(gen, "a") as f:
        for a in sorted(accounts, key=lambda a: a["Name"]):
            if not rx.search(a["Name"]):
                continue
            if a["Id"] in (login_acct, mgmt):
                say(f"  (skipping {a['Name']}: it is the management/login account)")
                continue
            prof = f"teardown-{a['Name']}"
            f.write(f"\n[profile {prof}]\nsso_session = {sso_session}\nsso_account_id = {a['Id']}\n"
                    f"sso_role_name = {role}\nregion = us-east-1\n")
            targets.append(Target(prof, a["Name"], a["Id"]))
            say(f"  {a['Name']} ({a['Id']})")
    os.environ["AWS_CONFIG_FILE"] = gen
    return targets
