import os

from sandbox_wipe import accounts


class FakeClient:
    def __init__(self, kind):
        self.kind = kind

    def get_caller_identity(self):
        return {"Account": "999999999999"}

    def describe_organization(self):
        return {"Organization": {"MasterAccountId": "999999999999"}}

    def get_paginator(self, _):
        return self

    def paginate(self):
        rows = [("sandbox01", "100000000001", "ACTIVE"), ("sandbox02", "100000000002", "ACTIVE"),
                ("sandbox03", "100000000003", "SUSPENDED"), ("mgmt-sandbox99", "999999999999", "ACTIVE"),
                ("other", "555555555555", "ACTIVE"), ("sandbox-notdigits", "100000000004", "ACTIVE")]
        return [{"Accounts": [{"Name": n, "Id": i, "Status": s} for n, i, s in rows]}]


class FakeSession:
    def __init__(self, profile_name=None):
        pass

    def client(self, name, **_):
        return FakeClient(name)


def test_resolve_matches_by_name_and_never_login_or_mgmt_account(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("[profile via]\nsso_session = s1\n[sso-session s1]\nsso_start_url = https://x\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(accounts.boto3, "Session", FakeSession)
    targets = accounts.resolve("via", r"^sandbox[0-9]+$", "RoleX", str(tmp_path / "out"), say=lambda *_: None)
    assert [t.name for t in targets] == ["sandbox01", "sandbox02"]
    text = (tmp_path / "out" / "aws-config").read_text()
    assert "teardown-sandbox01" in text and "sso_role_name = RoleX" in text and "sso_session = s1" in text
    assert "555555555555" not in text and "999999999999" not in text and "100000000003" not in text
    assert str(cfg) in text or "[profile via]" in text       # original config preserved in the generated copy
    assert cfg.read_text().count("teardown-") == 0           # the user's own config is untouched
    assert os.environ["AWS_CONFIG_FILE"].endswith("aws-config")
