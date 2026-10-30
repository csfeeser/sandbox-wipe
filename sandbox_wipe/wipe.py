"""Delete what the scanner found, phase by phase (dependency order), in parallel within a phase."""
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from botocore.exceptions import BotoCoreError, ClientError

from .scan import BOTO_CONFIG, Ctx

RETRIES = int(os.environ.get("SANDBOX_WIPE_RETRIES", "12"))
RETRY_DELAY = float(os.environ.get("SANDBOX_WIPE_RETRY_DELAY", "10"))
POLL_DELAY = float(os.environ.get("SANDBOX_WIPE_POLL_DELAY", "10"))
POLL_MAX = int(os.environ.get("SANDBOX_WIPE_POLL_MAX", "90"))   # polls (x POLL_DELAY) before giving up


def is_gone(code):
    return "NotFound" in code or code.startswith("NoSuch") or "NonExistent" in code or code == "ResourceNotFoundException"


def is_busy(code):
    return ("InUse" in code or "Dependency" in code or code in ("IncorrectState", "BucketNotEmpty", "OperationAborted")
            or (code.startswith("InvalidDB") and "State" in code) or code in ("ConcurrentModification", "IncorrectInstanceState"))


def call(fn, *a, **kw):
    """Run an API call; 'already gone' counts as success; 'still in use' is retried while AWS finishes other deletes."""
    for attempt in range(RETRIES + 1):
        try:
            return fn(*a, **kw)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if is_gone(code):
                return None
            if is_busy(code) and attempt < RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            raise


def poll_until(check, what):
    """check() returns True when finished. ClientError 'not found' also means finished."""
    for _ in range(POLL_MAX):
        try:
            if check():
                return
        except ClientError as e:
            if is_gone(e.response.get("Error", {}).get("Code", "")):
                return
            raise
        time.sleep(POLL_DELAY)
    raise TimeoutError(f"timed out waiting for {what}")


def waiter(client, name, attempts=120, **kw):
    try:
        client.get_waiter(name).wait(WaiterConfig={"Delay": max(int(POLL_DELAY), 1), "MaxAttempts": attempts}, **kw)
    except ClientError as e:
        if not is_gone(e.response.get("Error", {}).get("Code", "")):
            raise


# ---------------------------------------------------------------- step implementations: fn(ctx, res)

def cfn_delete(c, r):
    cf = c.client("cloudformation", r.region)
    call(cf.delete_stack, StackName=r.id)
    waiter(cf, "stack_delete_complete", StackName=r.id)


def asg_delete(c, r):
    a = c.client("autoscaling", r.region)
    call(a.delete_auto_scaling_group, AutoScalingGroupName=r.id, ForceDelete=True)
    poll_until(lambda: not a.describe_auto_scaling_groups(AutoScalingGroupNames=[r.id])["AutoScalingGroups"], r.id)


def eks_ng_delete(c, r):
    e = c.client("eks", r.region)
    call(e.delete_nodegroup, clusterName=r.extra["cluster"], nodegroupName=r.extra["ng"])
    waiter(e, "nodegroup_deleted", clusterName=r.extra["cluster"], nodegroupName=r.extra["ng"])


def eks_cluster_delete(c, r):
    e = c.client("eks", r.region)
    call(e.delete_cluster, name=r.id)
    waiter(e, "cluster_deleted", name=r.id)


def instance_terminate(c, r):
    ec2 = c.client("ec2", r.region)
    try:
        ec2.modify_instance_attribute(InstanceId=r.id, DisableApiTermination={"Value": False})
    except ClientError:
        pass
    call(ec2.terminate_instances, InstanceIds=[r.id])
    waiter(ec2, "instance_terminated", InstanceIds=[r.id])


def lb_delete(c, r):
    if r.type == "load-balancer-classic":
        return call(c.client("elb", r.region).delete_load_balancer, LoadBalancerName=r.id)
    v2 = c.client("elbv2", r.region)
    try:
        v2.modify_load_balancer_attributes(LoadBalancerArn=r.extra["arn"],
                                           Attributes=[{"Key": "deletion_protection.enabled", "Value": "false"}])
    except ClientError:
        pass
    call(v2.delete_load_balancer, LoadBalancerArn=r.extra["arn"])
    waiter(v2, "load_balancers_deleted", LoadBalancerArns=[r.extra["arn"]])


def tg_delete(c, r):
    call(c.client("elbv2", r.region).delete_target_group, TargetGroupArn=r.extra["arn"])


def rds_instance_delete(c, r):
    rds = c.client("rds", r.region)
    if r.extra.get("prot"):
        try:
            rds.modify_db_instance(DBInstanceIdentifier=r.id, DeletionProtection=False, ApplyImmediately=True)
        except ClientError:
            pass
    call(rds.delete_db_instance, DBInstanceIdentifier=r.id, SkipFinalSnapshot=True, DeleteAutomatedBackups=True)
    waiter(rds, "db_instance_deleted", DBInstanceIdentifier=r.id)


def rds_cluster_delete(c, r):
    rds = c.client("rds", r.region)
    if r.extra.get("prot"):
        try:
            rds.modify_db_cluster(DBClusterIdentifier=r.id, DeletionProtection=False, ApplyImmediately=True)
        except ClientError:
            pass
    call(rds.delete_db_cluster, DBClusterIdentifier=r.id, SkipFinalSnapshot=True)
    poll_until(lambda: not rds.describe_db_clusters(DBClusterIdentifier=r.id)["DBClusters"], r.id)


def rds_snapshot_delete(c, r):
    rds = c.client("rds", r.region)
    if r.type == "rds-cluster-snapshot":
        call(rds.delete_db_cluster_snapshot, DBClusterSnapshotIdentifier=r.id)
    else:
        call(rds.delete_db_snapshot, DBSnapshotIdentifier=r.id)


def rds_subnet_group_delete(c, r):
    call(c.client("rds", r.region).delete_db_subnet_group, DBSubnetGroupName=r.id)


def nat_delete(c, r):
    ec2 = c.client("ec2", r.region)
    call(ec2.delete_nat_gateway, NatGatewayId=r.id)
    poll_until(lambda: ec2.describe_nat_gateways(NatGatewayIds=[r.id])["NatGateways"][0]["State"] == "deleted", r.id)


def endpoint_delete(c, r):
    ec2 = c.client("ec2", r.region)
    call(ec2.delete_vpc_endpoints, VpcEndpointIds=[r.id])
    poll_until(lambda: not [e for e in ec2.describe_vpc_endpoints(VpcEndpointIds=[r.id])["VpcEndpoints"]
                            if e.get("State") not in ("deleted",)], r.id)


def eip_release(c, r):
    call(c.client("ec2", r.region).release_address, AllocationId=r.id)


def lambda_delete(c, r):
    call(c.client("lambda", r.region).delete_function, FunctionName=r.id)


def dynamodb_delete(c, r):
    d = c.client("dynamodb", r.region)
    try:
        if d.describe_table(TableName=r.id)["Table"].get("DeletionProtectionEnabled"):
            d.update_table(TableName=r.id, DeletionProtectionEnabled=False)
    except ClientError:
        pass
    call(d.delete_table, TableName=r.id)


def sqs_delete(c, r):
    call(c.client("sqs", r.region).delete_queue, QueueUrl=r.extra["url"])


def sns_delete(c, r):
    call(c.client("sns", r.region).delete_topic, TopicArn=r.extra["arn"])


def ecr_delete(c, r):
    call(c.client("ecr", r.region).delete_repository, repositoryName=r.id, force=True)


def ami_deregister(c, r):
    call(c.client("ec2", r.region).deregister_image, ImageId=r.id)


def snapshot_delete(c, r):
    call(c.client("ec2", r.region).delete_snapshot, SnapshotId=r.id)


def volume_delete(c, r):
    call(c.client("ec2", r.region).delete_volume, VolumeId=r.id)


def eni_delete(c, r):
    call(c.client("ec2", r.region).delete_network_interface, NetworkInterfaceId=r.id)


def sg_revoke(c, r):
    """Strip rules first so security groups that reference each other can all be deleted."""
    ec2 = c.client("ec2", r.region)
    try:
        g = ec2.describe_security_groups(GroupIds=[r.id])["SecurityGroups"][0]
    except ClientError as e:
        if is_gone(e.response.get("Error", {}).get("Code", "")):
            return
        raise
    if g.get("IpPermissions"):
        call(ec2.revoke_security_group_ingress, GroupId=r.id, IpPermissions=g["IpPermissions"])
    if any(p.get("UserIdGroupPairs") for p in g.get("IpPermissionsEgress", [])):
        call(ec2.revoke_security_group_egress, GroupId=r.id, IpPermissions=g["IpPermissionsEgress"])


def sg_delete(c, r):
    call(c.client("ec2", r.region).delete_security_group, GroupId=r.id)


def igw_delete(c, r):
    ec2 = c.client("ec2", r.region)
    if r.extra.get("vpc"):
        call(ec2.detach_internet_gateway, InternetGatewayId=r.id, VpcId=r.extra["vpc"])
    call(ec2.delete_internet_gateway, InternetGatewayId=r.id)


def subnet_delete(c, r):
    call(c.client("ec2", r.region).delete_subnet, SubnetId=r.id)


def rtb_delete(c, r):
    ec2 = c.client("ec2", r.region)
    try:
        for t in ec2.describe_route_tables(RouteTableIds=[r.id])["RouteTables"]:
            for a in t.get("Associations", []):
                if not a.get("Main"):
                    call(ec2.disassociate_route_table, AssociationId=a["RouteTableAssociationId"])
    except ClientError as e:
        if is_gone(e.response.get("Error", {}).get("Code", "")):
            return
        raise
    call(ec2.delete_route_table, RouteTableId=r.id)


def nacl_delete(c, r):
    call(c.client("ec2", r.region).delete_network_acl, NetworkAclId=r.id)


def vpc_delete(c, r):
    call(c.client("ec2", r.region).delete_vpc, VpcId=r.id)


def empty_and_delete_bucket(c, r):
    s3 = c.client("s3", r.extra["region"])
    for _ in range(4):
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=r.id):
            objs = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            for i in range(0, len(objs), 1000):
                resp = s3.delete_objects(Bucket=r.id, Delete={"Objects": objs[i:i + 1000], "Quiet": True})
                if resp.get("Errors"):
                    raise RuntimeError(f"could not delete objects: {resp['Errors'][0].get('Code')} {resp['Errors'][0].get('Message')}")
        for page in s3.get_paginator("list_multipart_uploads").paginate(Bucket=r.id):
            for u in page.get("Uploads", []):
                s3.abort_multipart_upload(Bucket=r.id, Key=u["Key"], UploadId=u["UploadId"])
        try:
            s3.delete_bucket(Bucket=r.id)
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if is_gone(code):
                return
            if code != "BucketNotEmpty":
                raise
    raise RuntimeError("bucket kept receiving new objects while being emptied")


STEPS = {
    ("cloudformation-stack", "delete"): cfn_delete, ("autoscaling-group", "delete"): asg_delete,
    ("eks-nodegroup", "delete"): eks_ng_delete, ("eks-cluster", "delete"): eks_cluster_delete,
    ("ec2-instance", "terminate"): instance_terminate,
    ("load-balancer", "delete"): lb_delete, ("load-balancer-classic", "delete"): lb_delete, ("target-group", "delete"): tg_delete,
    ("rds-instance", "delete"): rds_instance_delete, ("rds-cluster", "delete"): rds_cluster_delete,
    ("rds-snapshot", "delete"): rds_snapshot_delete, ("rds-cluster-snapshot", "delete"): rds_snapshot_delete,
    ("rds-subnet-group", "delete"): rds_subnet_group_delete,
    ("nat-gateway", "delete"): nat_delete, ("vpc-endpoint", "delete"): endpoint_delete, ("elastic-ip", "release"): eip_release,
    ("lambda", "delete"): lambda_delete, ("dynamodb-table", "delete"): dynamodb_delete, ("sqs-queue", "delete"): sqs_delete,
    ("sns-topic", "delete"): sns_delete, ("ecr-repository", "delete"): ecr_delete,
    ("ami", "deregister"): ami_deregister, ("ebs-snapshot", "delete"): snapshot_delete, ("ebs-volume", "delete"): volume_delete,
    ("network-interface", "delete"): eni_delete,
    ("security-group", "revoke"): sg_revoke, ("security-group", "delete"): sg_delete,
    ("internet-gateway", "delete"): igw_delete, ("subnet", "delete"): subnet_delete, ("route-table", "delete"): rtb_delete,
    ("network-acl", "delete"): nacl_delete, ("vpc", "delete"): vpc_delete, ("s3-bucket", "delete"): empty_and_delete_bucket,
}


def plan(resources):
    """[(phase, res, step)] in execution order. MANUAL items are never included."""
    steps = [(ph, r, st) for r in resources if not r.manual for ph, st in r.steps]
    steps.sort(key=lambda t: (t[0], t[1].region, t[1].type, t[1].id))
    return steps


def wipe_account(session, account_id, resources, threads=16, dry_run=False, log=None):
    """Returns (ok_count, failures[list of str]). `log(line)` receives one line per action."""
    log = log or (lambda s: None)
    steps = plan(resources)
    if dry_run:
        for ph, r, st in steps:
            log(f"PLAN  phase {ph:02d}  {st:<10} {r.type} {r.id} ({r.region})")
        return len(steps), []

    sts_acct = session.client("sts").get_caller_identity()["Account"]
    if sts_acct != account_id:
        raise RuntimeError(f"session is account {sts_acct}, expected {account_id}; refusing to delete")

    ctx = Ctx(session, None, [], threads)
    by_phase = defaultdict(list)
    for ph, r, st in steps:
        by_phase[ph].append((r, st))
    ok, failures = 0, []

    def run(item):
        r, st = item
        fn = STEPS.get((r.kind, st)) or STEPS.get((r.type, st))
        if not fn:
            return f"no handler for {r.type}/{st}"
        try:
            fn(ctx, r)
            return None
        except (ClientError, BotoCoreError, RuntimeError, TimeoutError) as e:
            code = e.response["Error"]["Code"] if isinstance(e, ClientError) else type(e).__name__
            return f"{code}: {e}"

    with ThreadPoolExecutor(threads) as pool:
        for ph in sorted(by_phase):
            items = by_phase[ph]
            for (r, st), err in zip(items, pool.map(run, items)):
                if err:
                    failures.append(f"{r.type} {r.id} ({r.region}) {st}: {err}")
                    log(f"FAILED phase {ph:02d}  {st:<10} {r.type} {r.id} ({r.region})  {err}")
                else:
                    ok += 1
                    log(f"OK     phase {ph:02d}  {st:<10} {r.type} {r.id} ({r.region})")
    return ok, failures
