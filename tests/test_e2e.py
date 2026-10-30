"""End-to-end tests against moto (a simulated AWS): build a mess, scan it, wipe it, check what survived."""
import io
import json
import os
import zipfile

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from sandbox_wipe import cli, scan

REGIONS = ["us-east-1", "us-west-2"]
ARGS = ["--profile", "default", "--non-interactive", "--regions", ",".join(REGIONS)]


def ec2(r="us-east-1"):
    return boto3.client("ec2", region_name=r)


@pytest.fixture
def world(tmp_path):
    """A class-sized mess in us-east-1/us-west-2, plus things that must be left alone."""
    with mock_aws():
        e = ec2()
        w = {}
        w["default_vpc"] = e.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
        vpc = e.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        e.create_tags(Resources=[vpc], Tags=[{"Key": "Name", "Value": "student lab"}])
        sub = e.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
        igw = e.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
        e.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
        rtb = e.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
        e.associate_route_table(RouteTableId=rtb, SubnetId=sub)
        sg1 = e.create_security_group(GroupName="web", Description="d", VpcId=vpc)["GroupId"]
        sg2 = e.create_security_group(GroupName="db", Description="d", VpcId=vpc)["GroupId"]
        e.authorize_security_group_ingress(GroupId=sg1, IpPermissions=[{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                                           "UserIdGroupPairs": [{"GroupId": sg2}]}])
        e.authorize_security_group_ingress(GroupId=sg2, IpPermissions=[{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                                           "UserIdGroupPairs": [{"GroupId": sg1}]}])
        ami = e.register_image(Name="class-base", Architecture="x86_64", RootDeviceName="/dev/sda1", VirtualizationType="hvm")["ImageId"]
        w["inst"] = e.run_instances(ImageId=ami, MinCount=1, MaxCount=1, InstanceType="t3.micro", SubnetId=sub,
                                    TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "testy-web"}]}]
                                    )["Instances"][0]["InstanceId"]
        e.create_volume(AvailabilityZone="us-east-1a", Size=8)
        e.allocate_address(Domain="vpc")
        # --- must survive
        w["keep_vpc"] = e.create_vpc(CidrBlock="10.9.0.0/16")["Vpc"]["VpcId"]
        e.create_tags(Resources=[w["keep_vpc"]], Tags=[{"Key": "Keep", "Value": "true"}])
        w["keep_sub"] = e.create_subnet(VpcId=w["keep_vpc"], CidrBlock="10.9.1.0/24")["Subnet"]["SubnetId"]
        w["keep_inst"] = e.run_instances(ImageId=ami, MinCount=1, MaxCount=1, SubnetId=w["keep_sub"],
                                         TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Keep", "Value": "true"}]}]
                                         )["Instances"][0]["InstanceId"]
        w["vpc"] = vpc
        # --- other services
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="tfstate-111111111111")
        s3.put_bucket_versioning(Bucket="tfstate-111111111111", VersioningConfiguration={"Status": "Enabled"})
        for i in range(3):
            s3.put_object(Bucket="tfstate-111111111111", Key="terraform.tfstate", Body=b"v%d" % i)
        s3.delete_object(Bucket="tfstate-111111111111", Key="terraform.tfstate")
        s3.create_bucket(Bucket="aws-controltower-logs")
        s3.create_bucket(Bucket="keepme")
        s3.put_bucket_tagging(Bucket="keepme", Tagging={"TagSet": [{"Key": "Keep", "Value": "true"}]})
        boto3.client("sqs", region_name="us-east-1").create_queue(QueueName="q1")
        boto3.client("sns", region_name="us-west-2").create_topic(Name="t1")
        boto3.client("dynamodb", region_name="us-west-2").create_table(
            TableName="tbl", KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        boto3.client("rds", region_name="us-west-2").create_db_instance(
            DBInstanceIdentifier="db-1", DBInstanceClass="db.t3.micro", Engine="mysql", MasterUsername="a",
            MasterUserPassword="password123", AllocatedStorage=5)
        yield w


def run(*extra, tmp_path, capsys=None):
    return cli.main(ARGS + ["--out", str(tmp_path / "out")] + list(extra))


def summary(tmp_path):
    return json.load(open(tmp_path / "out" / "summary.json"))["accounts"][0]


def types(tmp_path):
    return sorted({r["type"] for r in summary(tmp_path)["resources"]})


def ids(tmp_path):
    return {r["id"] for r in summary(tmp_path)["resources"]}


def test_scan_finds_the_mess_and_skips_protected(world, tmp_path):
    assert run("scan", tmp_path=tmp_path) == 4
    got = ids(tmp_path)
    assert {world["inst"], world["vpc"], "tfstate-111111111111", "q1", "t1", "tbl", "db-1"} <= got
    assert {world["keep_inst"], world["keep_vpc"], world["keep_sub"], "keepme", "aws-controltower-logs", world["default_vpc"]}.isdisjoint(got)
    assert {"subnet", "route-table", "internet-gateway", "security-group", "ebs-volume", "elastic-ip", "ami"} <= set(types(tmp_path))


def test_default_vpc_resources_never_targeted(world, tmp_path):
    run("scan", tmp_path=tmp_path)
    for r in summary(tmp_path)["resources"]:
        assert r["detail"] != world["default_vpc"] or r["type"] == "nat-gateway"
    default_subnets = {s["SubnetId"] for s in ec2().describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [world["default_vpc"]]}])["Subnets"]}
    assert default_subnets.isdisjoint(ids(tmp_path))


def test_wipe_refuses_without_yes(world, tmp_path):
    assert run("wipe", tmp_path=tmp_path) == 3
    assert ec2().describe_instances(InstanceIds=[world["inst"]])["Reservations"][0]["Instances"][0]["State"]["Name"] == "running"


def test_dry_run_changes_nothing_and_lists_steps(world, tmp_path):
    assert run("wipe", "--dry-run", tmp_path=tmp_path) == 0
    plan = open(next((tmp_path / "out").glob("*/plan.log"))).read()
    assert "terminate" in plan and "delete" in plan
    assert ec2().describe_instances(InstanceIds=[world["inst"]])["Reservations"][0]["Instances"][0]["State"]["Name"] == "running"


def test_wipe_deletes_everything_and_spares_kept(world, tmp_path):
    assert run("wipe", "--yes", tmp_path=tmp_path) == 0
    e = ec2()
    assert e.describe_instances(InstanceIds=[world["inst"]])["Reservations"][0]["Instances"][0]["State"]["Name"] == "terminated"
    assert world["vpc"] not in [v["VpcId"] for v in e.describe_vpcs()["Vpcs"]]
    survivors = {v["VpcId"] for v in e.describe_vpcs()["Vpcs"]}
    assert {world["default_vpc"], world["keep_vpc"]} <= survivors
    assert e.describe_instances(InstanceIds=[world["keep_inst"]])["Reservations"][0]["Instances"][0]["State"]["Name"] == "running"
    buckets = {b["Name"] for b in boto3.client("s3").list_buckets()["Buckets"]}
    assert buckets == {"aws-controltower-logs", "keepme"}
    assert boto3.client("sqs", region_name="us-east-1").list_queues().get("QueueUrls", []) == []
    assert boto3.client("rds", region_name="us-west-2").describe_db_instances()["DBInstances"] == []
    assert run("scan", tmp_path=tmp_path) == 0


def test_fast_scans_only_ec2_and_s3(world, tmp_path):
    run("scan", "--fast", tmp_path=tmp_path)
    t = set(types(tmp_path))
    assert "ec2-instance" in t and "s3-bucket" in t
    assert t.isdisjoint({"sqs-queue", "sns-topic", "dynamodb-table", "rds-instance"})


def test_vpc_lookup_failure_fails_closed(world, tmp_path, monkeypatch):
    def denied(c, r):
        raise ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "no"}}, "DescribeVpcs")
    monkeypatch.setattr(scan, "fetch_vpcs", denied)
    run("scan", tmp_path=tmp_path)
    t = set(types(tmp_path))
    assert "ec2-instance" in t                       # instances do not need the VPC list
    assert t.isdisjoint({"vpc", "subnet", "security-group", "internet-gateway", "route-table", "nat-gateway"})
    assert "ec2-vpcs" in summary(tmp_path)["coverage_gaps"]


def test_denied_services_are_reported_as_gaps_not_errors(world, tmp_path, monkeypatch):
    def denied(c, r):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DescribeDBInstances")
    monkeypatch.setattr(scan, "scan_rds", denied)
    monkeypatch.setattr(scan, "REGION_TASKS", [(l, g, denied if l == "rds" else f) for l, g, f in scan.REGION_TASKS])
    run("scan", tmp_path=tmp_path)
    s = summary(tmp_path)
    assert "rds" in s["coverage_gaps"] and s["errors"] == 0


def test_unknown_service_rejected(tmp_path):
    with pytest.raises(SystemExit) as e:
        run("scan", "--services", "bogus", tmp_path=tmp_path)
    assert e.value.code == 1


class FakeTTY:
    def isatty(self):
        return True


def _interactive(monkeypatch, answers):
    import builtins
    import types
    import sys as real_sys
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))
    monkeypatch.setattr(cli, "sys", types.SimpleNamespace(stdin=FakeTTY(), stdout=FakeTTY(), stderr=real_sys.stderr))


def test_interactive_wipe_cancelled_unless_WIPE_typed(world, tmp_path, monkeypatch):
    _interactive(monkeypatch, ["", "n", "nope"])           # regions, fast scan?, confirmation
    assert cli.main(["wipe", "--profile", "default", "--regions", ",".join(REGIONS), "--out", str(tmp_path / "out")]) == 0
    assert ec2().describe_instances(InstanceIds=[world["inst"]])["Reservations"][0]["Instances"][0]["State"]["Name"] == "running"


def test_interactive_menu_then_wipe(world, tmp_path, monkeypatch):
    _interactive(monkeypatch, ["2", "", "y", "WIPE"])      # menu choice, regions, fast scan?, confirmation
    assert cli.main(["--profile", "default", "--regions", ",".join(REGIONS), "--out", str(tmp_path / "out")]) == 0
    assert ec2().describe_instances(InstanceIds=[world["inst"]])["Reservations"][0]["Instances"][0]["State"]["Name"] == "terminated"
    assert "tfstate-111111111111" not in {b["Name"] for b in boto3.client("s3").list_buckets()["Buckets"]}
