"""Shared data types and helpers."""
import re
import threading
from dataclasses import dataclass, field

DEFAULT_PROTECT = (
    r"^(AWSControlTower|aws-controltower|StackSet-|AWSReservedSSO|cf-templates-"
    r"|aws-cloudtrail-logs|config-bucket-|cdk-hnb659fds)"
)
ALL_SERVICES = ("cfn", "asg", "eks", "ec2", "elb", "rds", "serverless", "s3", "iam")
FAST_SERVICES = ("ec2", "s3")
DENIED_CODES = {
    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "AuthorizationError",
    "UnauthorizedAccess", "AccessDeniedFault", "OptInRequired", "AuthFailure", "UnrecognizedClientException",
}


@dataclass
class Res:
    """One thing found in an account. `steps` = ((phase, step_name), ...) in deletion order."""
    region: str
    type: str
    id: str
    detail: str = ""
    steps: tuple = ()
    extra: dict = field(default_factory=dict)

    @property
    def manual(self):
        return self.type.startswith("MANUAL:")

    @property
    def kind(self):
        return "load-balancer" if self.type.startswith("load-balancer-") and self.type != "load-balancer-classic" else self.type

    def to_json(self):
        return {"region": self.region, "type": self.type, "id": self.id, "detail": self.detail}


@dataclass
class VpcInfo:
    ok: bool = False
    default: str = ""
    kept: set = field(default_factory=set)


@dataclass
class AccountResult:
    profile: str
    account: str = ""
    status: str = "ok"          # ok | skipped
    reason: str = ""
    dir: str = ""
    resources: list = field(default_factory=list)
    gaps: list = field(default_factory=list)      # services the login was not allowed to look at
    errors: list = field(default_factory=list)    # unexpected API errors

    @property
    def destroyable(self):
        return sum(1 for r in self.resources if not r.manual)

    @property
    def manual(self):
        return sum(1 for r in self.resources if r.manual)

    def to_json(self):
        if self.status != "ok":
            return {"profile": self.profile, "status": "skipped", "reason": self.reason}
        return {
            "profile": self.profile, "account": self.account, "status": "ok", "dir": self.dir,
            "destroyable": self.destroyable, "manual": self.manual,
            "coverage_gaps": sorted(self.gaps), "errors": len(self.errors),
            "resources": [r.to_json() for r in self.resources],
        }


class Matcher:
    """Name protection + Keep-tag logic."""

    def __init__(self, protect=None, keep_tag="Keep"):
        self.rx = re.compile(protect or DEFAULT_PROTECT)
        self.keep_tag = keep_tag

    def protected(self, name):
        if not name:
            return False
        return bool(self.rx.search(re.split(r"[/:]", name)[-1]))

    def kept(self, tags):
        return any(t.get("Key") == self.keep_tag and str(t.get("Value", "")).lower() == "true" for t in tags or [])


def tag_name(tags):
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", "")
    return ""


class Counter:
    def __init__(self):
        self.lock = threading.Lock()
        self.n = 0

    def inc(self):
        with self.lock:
            self.n += 1
