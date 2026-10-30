"""Inventory one AWS account. Every API question runs in a thread pool inside this one process."""
import threading
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .model import DENIED_CODES, AccountResult, Matcher, Res, VpcInfo, tag_name

BOTO_CONFIG = Config(retries={"max_attempts": 10, "mode": "standard"}, connect_timeout=10,
                     read_timeout=60, max_pool_connections=64)


class Ctx:
    """Everything a scan task needs. Clients are cached; creating them is serialised (Sessions are not thread safe)."""

    def __init__(self, session, matcher, regions, threads):
        self.session, self.m, self.regions, self.threads = session, matcher, regions, threads
        self._clients, self._lock = {}, threading.Lock()
        self.vpc = {}

    def client(self, service, region):
        key = (service, region)
        with self._lock:
            if key not in self._clients:
                self._clients[key] = self.session.client(service, region_name=region, config=BOTO_CONFIG)
            return self._clients[key]


def pages(client, op, key, **kw):
    """Yield items under `key` across all pages (or the single response if the op cannot paginate)."""
    if client.can_paginate(op):
        for page in client.get_paginator(op).paginate(**kw):
            yield from page.get(key, [])
    else:
        yield from getattr(client, op)(**kw).get(key, [])


# ---------------------------------------------------------------- per-region tasks (return list[Res])

def scan_cfn(c, r):
    cf = c.client("cloudformation", r)
    st = ["CREATE_COMPLETE", "UPDATE_COMPLETE", "ROLLBACK_COMPLETE", "CREATE_FAILED", "UPDATE_ROLLBACK_COMPLETE",
          "DELETE_FAILED", "ROLLBACK_FAILED", "UPDATE_ROLLBACK_FAILED", "IMPORT_COMPLETE", "IMPORT_ROLLBACK_COMPLETE"]
    return [Res(r, "cloudformation-stack", s["StackName"], steps=((3, "delete"),))
            for s in pages(cf, "list_stacks", "StackSummaries", StackStatusFilter=st)
            if not s.get("ParentId") and not c.m.protected(s["StackName"])]


def scan_asg(c, r):
    a = c.client("autoscaling", r)
    return [Res(r, "autoscaling-group", g["AutoScalingGroupName"], steps=((5, "delete"),))
            for g in pages(a, "describe_auto_scaling_groups", "AutoScalingGroups")
            if not c.m.protected(g["AutoScalingGroupName"])]


def scan_eks(c, r):
    e, out = c.client("eks", r), []
    for name in pages(e, "list_clusters", "clusters"):
        if c.m.protected(name):
            continue
        for ng in pages(e, "list_nodegroups", "nodegroups", clusterName=name):
            out.append(Res(r, "eks-nodegroup", f"{name}/{ng}", steps=((6, "delete"),), extra={"cluster": name, "ng": ng}))
        out.append(Res(r, "eks-cluster", name, steps=((8, "delete"),)))
    return out


def scan_instances(c, r):
    ec2, out = c.client("ec2", r), []
    flt = [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    for res in pages(ec2, "describe_instances", "Reservations", Filters=flt):
        for i in res["Instances"]:
            nm = tag_name(i.get("Tags"))
            if c.m.kept(i.get("Tags")) or c.m.protected(nm):
                continue
            out.append(Res(r, "ec2-instance", i["InstanceId"], f"{i['InstanceType']} {i['State']['Name']} {nm}".strip(),
                           steps=((10, "terminate"),)))
    return out


def scan_eip(c, r):
    out = []
    for a in c.client("ec2", r).describe_addresses().get("Addresses", []):
        if a.get("AllocationId") and not c.m.kept(a.get("Tags")):
            out.append(Res(r, "elastic-ip", a["AllocationId"], a.get("PublicIp", ""), steps=((31, "release"),)))
    return out


def scan_storage(c, r):
    ec2, out = c.client("ec2", r), []
    for i in ec2.describe_images(Owners=["self"]).get("Images", []):
        if not c.m.protected(i.get("Name", "")):
            out.append(Res(r, "ami", i["ImageId"], i.get("Name", ""), steps=((41, "deregister"),)))
    for s in pages(ec2, "describe_snapshots", "Snapshots", OwnerIds=["self"]):
        if not c.m.kept(s.get("Tags")):
            out.append(Res(r, "ebs-snapshot", s["SnapshotId"], f"{s.get('VolumeSize', '?')}GB", steps=((42, "delete"),)))
    for v in pages(ec2, "describe_volumes", "Volumes", Filters=[{"Name": "status", "Values": ["available"]}]):
        nm = tag_name(v.get("Tags"))
        if not c.m.kept(v.get("Tags")) and not c.m.protected(nm):
            out.append(Res(r, "ebs-volume", v["VolumeId"], f"{v['Size']}GB {nm}".strip(), steps=((43, "delete"),)))
    return out


def scan_manual_net(c, r):
    ec2, out = c.client("ec2", r), []
    for t in pages(ec2, "describe_transit_gateways", "TransitGateways"):
        if t["State"] != "deleted":
            out.append(Res(r, "MANUAL:transit-gateway", t["TransitGatewayId"], "not handled"))
    flt = [{"Name": "status-code", "Values": ["active", "pending-acceptance"]}]
    for p in pages(ec2, "describe_vpc_peering_connections", "VpcPeeringConnections", Filters=flt):
        out.append(Res(r, "MANUAL:vpc-peering", p["VpcPeeringConnectionId"], "not handled"))
    return out


def scan_elb(c, r):
    out = []
    v2 = c.client("elbv2", r)
    for lb in pages(v2, "describe_load_balancers", "LoadBalancers"):
        if not c.m.protected(lb["LoadBalancerName"]):
            out.append(Res(r, f"load-balancer-{lb.get('Type', 'application')}", lb["LoadBalancerName"], lb["LoadBalancerArn"],
                           steps=((15, "delete"),), extra={"arn": lb["LoadBalancerArn"]}))
    for tg in pages(v2, "describe_target_groups", "TargetGroups"):
        if not c.m.protected(tg["TargetGroupName"]):
            out.append(Res(r, "target-group", tg["TargetGroupName"], tg["TargetGroupArn"],
                           steps=((17, "delete"),), extra={"arn": tg["TargetGroupArn"]}))
    return out


def scan_elb_classic(c, r):
    return [Res(r, "load-balancer-classic", lb["LoadBalancerName"], steps=((15, "delete"),))
            for lb in pages(c.client("elb", r), "describe_load_balancers", "LoadBalancerDescriptions")
            if not c.m.protected(lb["LoadBalancerName"])]


def scan_rds(c, r):
    rds, out = c.client("rds", r), []
    for d in pages(rds, "describe_db_instances", "DBInstances"):
        i = d["DBInstanceIdentifier"]
        if not c.m.protected(i):
            out.append(Res(r, "rds-instance", i, f"{d.get('Engine', '')} {d.get('DBInstanceClass', '')}".strip(),
                           steps=((20, "delete"),), extra={"prot": bool(d.get("DeletionProtection"))}))
    for d in pages(rds, "describe_db_clusters", "DBClusters"):
        i = d["DBClusterIdentifier"]
        if not c.m.protected(i):
            out.append(Res(r, "rds-cluster", i, d.get("Engine", ""), steps=((23, "delete"),),
                           extra={"prot": bool(d.get("DeletionProtection"))}))
    for d in pages(rds, "describe_db_snapshots", "DBSnapshots", SnapshotType="manual"):
        out.append(Res(r, "rds-snapshot", d["DBSnapshotIdentifier"], steps=((25, "delete"),)))
    for d in pages(rds, "describe_db_cluster_snapshots", "DBClusterSnapshots", SnapshotType="manual"):
        out.append(Res(r, "rds-cluster-snapshot", d["DBClusterSnapshotIdentifier"], steps=((25, "delete"),)))
    for d in pages(rds, "describe_db_subnet_groups", "DBSubnetGroups"):
        n = d["DBSubnetGroupName"]
        if not n.startswith("default") and not c.m.protected(n):
            out.append(Res(r, "rds-subnet-group", n, steps=((26, "delete"),)))
    return out


def scan_lambda(c, r):
    return [Res(r, "lambda", f["FunctionName"], steps=((35, "delete"),))
            for f in pages(c.client("lambda", r), "list_functions", "Functions") if not c.m.protected(f["FunctionName"])]


def scan_dynamodb(c, r):
    return [Res(r, "dynamodb-table", t, steps=((36, "delete"),))
            for t in pages(c.client("dynamodb", r), "list_tables", "TableNames") if not c.m.protected(t)]


def scan_sqs(c, r):
    return [Res(r, "sqs-queue", u.rsplit("/", 1)[-1], u, steps=((37, "delete"),), extra={"url": u})
            for u in pages(c.client("sqs", r), "list_queues", "QueueUrls") if not c.m.protected(u.rsplit("/", 1)[-1])]


def scan_sns(c, r):
    return [Res(r, "sns-topic", t["TopicArn"].rsplit(":", 1)[-1], t["TopicArn"], steps=((38, "delete"),),
                extra={"arn": t["TopicArn"]})
            for t in pages(c.client("sns", r), "list_topics", "Topics") if not c.m.protected(t["TopicArn"])]


def scan_ecr(c, r):
    return [Res(r, "ecr-repository", x["repositoryName"], steps=((39, "delete"),))
            for x in pages(c.client("ecr", r), "describe_repositories", "repositories") if not c.m.protected(x["repositoryName"])]


# --- network: everything below needs the VPC list (default VPC + kept VPCs)

def scan_nat(c, r):
    out = []
    flt = [{"Name": "state", "Values": ["pending", "available"]}]
    for n in pages(c.client("ec2", r), "describe_nat_gateways", "NatGateways", Filter=flt):
        if n["VpcId"] not in c.vpc[r].kept:
            out.append(Res(r, "nat-gateway", n["NatGatewayId"], n["VpcId"], steps=((27, "delete"),)))
    return out


def scan_endpoints(c, r):
    out = []
    flt = [{"Name": "vpc-endpoint-state", "Values": ["available", "pending", "pendingAcceptance"]}]
    for e in pages(c.client("ec2", r), "describe_vpc_endpoints", "VpcEndpoints", Filters=flt):
        if e["VpcId"] not in c.vpc[r].kept:
            out.append(Res(r, "vpc-endpoint", e["VpcEndpointId"], e.get("ServiceName", ""), steps=((29, "delete"),)))
    return out


def scan_enis(c, r):
    flt = [{"Name": "status", "Values": ["available"]}]
    return [Res(r, "network-interface", n["NetworkInterfaceId"], n.get("VpcId", ""), steps=((55, "delete"),))
            for n in pages(c.client("ec2", r), "describe_network_interfaces", "NetworkInterfaces", Filters=flt)
            if n.get("VpcId") not in c.vpc[r].kept]


def scan_sgs(c, r):
    return [Res(r, "security-group", g["GroupId"], g["GroupName"], steps=((60, "revoke"), (76, "delete")))
            for g in pages(c.client("ec2", r), "describe_security_groups", "SecurityGroups")
            if g["GroupName"] != "default" and g.get("VpcId") not in c.vpc[r].kept and not c.m.protected(g["GroupName"])]


def scan_igws(c, r):
    out = []
    for g in pages(c.client("ec2", r), "describe_internet_gateways", "InternetGateways"):
        vpc = (g.get("Attachments") or [{}])[0].get("VpcId", "")
        if vpc and (vpc == c.vpc[r].default or vpc in c.vpc[r].kept):
            continue
        out.append(Res(r, "internet-gateway", g["InternetGatewayId"], vpc, steps=((65, "delete"),), extra={"vpc": vpc}))
    return out


def scan_subnets(c, r):
    return [Res(r, "subnet", s["SubnetId"], f"{s['VpcId']} {s['CidrBlock']}", steps=((70, "delete"),))
            for s in pages(c.client("ec2", r), "describe_subnets", "Subnets")
            if not s.get("DefaultForAz") and s["VpcId"] not in c.vpc[r].kept]


def scan_rtbs(c, r):
    return [Res(r, "route-table", t["RouteTableId"], t["VpcId"], steps=((72, "delete"),))
            for t in pages(c.client("ec2", r), "describe_route_tables", "RouteTables")
            if not any(a.get("Main") for a in t.get("Associations", [])) and t["VpcId"] not in c.vpc[r].kept]


def scan_nacls(c, r):
    return [Res(r, "network-acl", n["NetworkAclId"], n["VpcId"], steps=((74, "delete"),))
            for n in pages(c.client("ec2", r), "describe_network_acls", "NetworkAcls")
            if not n.get("IsDefault") and n["VpcId"] not in c.vpc[r].kept]


def scan_vpcs(c, r):
    out = []
    for v in pages(c.client("ec2", r), "describe_vpcs", "Vpcs"):
        if v.get("IsDefault") or v["VpcId"] in c.vpc[r].kept:
            continue
        out.append(Res(r, "vpc", v["VpcId"], f"{v['CidrBlock']} {tag_name(v.get('Tags'))}".strip(), steps=((80, "delete"),)))
    return out


# (label, service group, function). Group "ec2net" = ec2 + a successful VPC lookup.
REGION_TASKS = [
    ("cloudformation", "cfn", scan_cfn), ("autoscaling", "asg", scan_asg), ("eks", "eks", scan_eks),
    ("ec2-instances", "ec2", scan_instances), ("ec2-addresses", "ec2", scan_eip), ("ec2-storage", "ec2", scan_storage),
    ("ec2-transit/peering", "ec2", scan_manual_net),
    ("elbv2", "elb", scan_elb), ("elb-classic", "elb", scan_elb_classic),
    ("rds", "rds", scan_rds),
    ("lambda", "serverless", scan_lambda), ("dynamodb", "serverless", scan_dynamodb), ("sqs", "serverless", scan_sqs),
    ("sns", "serverless", scan_sns), ("ecr", "serverless", scan_ecr),
    ("nat-gateways", "ec2net", scan_nat), ("vpc-endpoints", "ec2net", scan_endpoints),
    ("network-interfaces", "ec2net", scan_enis), ("security-groups", "ec2net", scan_sgs),
    ("internet-gateways", "ec2net", scan_igws), ("subnets", "ec2net", scan_subnets),
    ("route-tables", "ec2net", scan_rtbs), ("network-acls", "ec2net", scan_nacls), ("vpcs", "ec2net", scan_vpcs),
]


# ---------------------------------------------------------------- global tasks (S3, IAM)

def s3_bucket_region(s3, name):
    loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
    return {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(loc, loc)


def scan_s3(c, _r):
    s3 = c.client("s3", "us-east-1")
    out = []
    for b in s3.list_buckets().get("Buckets", []):
        name = b["Name"]
        if c.m.protected(name):
            continue
        region = s3_bucket_region(s3, name)
        try:
            tags = c.client("s3", region).get_bucket_tagging(Bucket=name)["TagSet"]
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("NoSuchTagSet", "NoSuchTagSetError"):
                raise
            tags = []
        if c.m.kept(tags):
            continue
        out.append(Res("global", "s3-bucket", name, steps=((85, "delete"),), extra={"region": region}))
    return out


def scan_iam(c, _r):
    iam, out = c.client("iam", "us-east-1"), []
    for role in pages(iam, "list_roles", "Roles"):
        if not role["Path"].startswith(("/aws-service-role/", "/aws-reserved/")) and not c.m.protected(role["RoleName"]):
            out.append(Res("global", "MANUAL:iam-role", role["RoleName"], "not deleted by this tool; review by hand"))
    for u in pages(iam, "list_users", "Users"):
        if not c.m.protected(u["UserName"]):
            out.append(Res("global", "MANUAL:iam-user", u["UserName"], "not deleted by this tool; review by hand"))
    return out


GLOBAL_TASKS = [("s3", "s3", scan_s3), ("iam", "iam", scan_iam)]


# ---------------------------------------------------------------- driver

def fetch_vpcs(c, r):
    """The default VPC and 'kept' VPCs decide what the network scanners may touch. Fail closed if unknown."""
    info = VpcInfo()
    for v in pages(c.client("ec2", r), "describe_vpcs", "Vpcs"):
        if v.get("IsDefault"):
            info.default = v["VpcId"]
        if c.m.kept(v.get("Tags")) or c.m.protected(tag_name(v.get("Tags"))):
            info.kept.add(v["VpcId"])
    info.ok = True
    return info


def scan_account(session, profile, account_id, regions, services, keep_tag="Keep", protect=None, threads=16):
    res = AccountResult(profile=profile, account=account_id)
    c = Ctx(session, Matcher(protect, keep_tag), regions, threads)
    lock = threading.Lock()

    def guarded(label, fn, r):
        try:
            return fn(c, r)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            with lock:
                (res.gaps.append(label) if code in DENIED_CODES else res.errors.append(f"{label} {r}: {code}"))
        except BotoCoreError as e:
            with lock:
                res.errors.append(f"{label} {r}: {type(e).__name__}: {e}")
        return []

    with ThreadPoolExecutor(threads) as pool:
        if "ec2" in services:
            def vpc_task(r):
                try:
                    c.vpc[r] = fetch_vpcs(c, r)
                except (ClientError, BotoCoreError) as e:
                    c.vpc[r] = VpcInfo()
                    code = e.response.get("Error", {}).get("Code", "") if isinstance(e, ClientError) else type(e).__name__
                    with lock:
                        (res.gaps.append("ec2-vpcs") if code in DENIED_CODES else res.errors.append(f"vpc lookup {r}: {code}"))
            list(pool.map(vpc_task, regions))

        futures = []
        for r in regions:
            for label, group, fn in REGION_TASKS:
                if group == "ec2net":
                    if "ec2" not in services or not c.vpc.get(r, VpcInfo()).ok:
                        continue
                elif group not in services:
                    continue
                futures.append(pool.submit(guarded, label, fn, r))
        for label, group, fn in GLOBAL_TASKS:
            if group in services:
                futures.append(pool.submit(guarded, label, fn, "global"))
        for f in futures:
            res.resources += f.result()

    res.resources.sort(key=lambda x: (x.region, x.type, x.id))
    res.gaps = sorted(set(res.gaps))
    return res
