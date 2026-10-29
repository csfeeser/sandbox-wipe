#!/usr/bin/env bash
# discover.sh - inventory leftover AWS resources and GENERATE a reviewable destroy script.
# This script is read-only: it never deletes anything. The generated destroy.sh does the deleting.
set -uo pipefail

PROFILES=()
REGIONS=""
OUTDIR="./out"
KEEP_TAG="Keep"
MAXJOBS=6
ACCOUNT_JOBS=4
ACCOUNTS_REGEX=""
VIA=""
ROLE="TerraformStudentLabs"
GEN_CONFIG=""
PROTECT_REGEX='^(AWSControlTower|aws-controltower|StackSet-|AWSReservedSSO|cf-templates-|aws-cloudtrail-logs|config-bucket-|cdk-hnb659fds)'

usage() {
  cat <<EOF
Usage: $0 (-p PROFILE ... | -a REGEX --via PROFILE) [options]

  -p, --profile PROFILE   AWS CLI profile (one per account). Repeatable.
  -a, --accounts-regex RE Select accounts BY NAME (e.g. '^sandbox[0-9]+\$') using AWS Organizations.
                          Needs --via. Temporary SSO profiles are written to DIR/aws-config;
                          your ~/.aws/config is not modified.
      --via PROFILE       SSO profile allowed to run organizations:ListAccounts (e.g. sandbox-creator)
      --role NAME         SSO permission set to use in each matched account (default: TerraformStudentLabs)
  -r, --regions R1,R2     Regions to scan (default: every enabled region)
  -o, --out DIR           Output directory (default: ./out)
  -k, --keep-tag KEY      Resources tagged KEY=true are never targeted (default: Keep)
  -x, --protect REGEX     Names matching this regex are never targeted
                          (default protects Control Tower / SSO / CDK bootstrap names)
  -j, --jobs N            Regions scanned in parallel per account (default: 6)
  -J, --account-jobs N    Accounts scanned in parallel (default: 4)

Output, per account, in DIR/<account-id>-<profile>/:
  inventory.tsv   every resource found (region, type, id, detail)
  destroy.sh      commands to delete them; dry-run unless run with --execute
EOF
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case $1 in
    -p|--profile) PROFILES+=("$2"); shift 2 ;;
    -r|--regions) REGIONS=${2//,/ }; shift 2 ;;
    -o|--out) OUTDIR=$2; shift 2 ;;
    -k|--keep-tag) KEEP_TAG=$2; shift 2 ;;
    -x|--protect) PROTECT_REGEX=$2; shift 2 ;;
    -j|--jobs) MAXJOBS=$2; shift 2 ;;
    -J|--account-jobs) ACCOUNT_JOBS=$2; shift 2 ;;
    -a|--accounts-regex) ACCOUNTS_REGEX=$2; shift 2 ;;
    --via) VIA=$2; shift 2 ;;
    --role) ROLE=$2; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done
for tool in aws jq; do command -v "$tool" >/dev/null || { echo "$tool not found" >&2; exit 1; }; done

if [ -n "$ACCOUNTS_REGEX" ]; then
  [ -n "$VIA" ] || { echo "--accounts-regex needs --via PROFILE" >&2; exit 1; }
  mkdir -p "$OUTDIR"; OUTDIR=$(cd "$OUTDIR" && pwd)
  session=$(aws configure get sso_session --profile "$VIA")
  [ -n "$session" ] || { echo "profile $VIA has no sso_session; cannot derive per-account SSO profiles" >&2; exit 1; }
  accts=$(aws organizations list-accounts --profile "$VIA" --query 'Accounts[?Status==`ACTIVE`].[Name,Id]' --output text | sort) \
    || { echo "could not list accounts via $VIA (aws sso login --profile $VIA?)" >&2; exit 1; }
  via_acct=$(aws sts get-caller-identity --profile "$VIA" --query Account --output text 2>/dev/null)
  mgmt_acct=$(aws organizations describe-organization --profile "$VIA" --query Organization.MasterAccountId --output text 2>/dev/null)
  GEN_CONFIG="$OUTDIR/aws-config"
  cp "${AWS_CONFIG_FILE:-$HOME/.aws/config}" "$GEN_CONFIG"
  echo "Accounts matching $ACCOUNTS_REGEX:"
  while IFS=$'\t' read -r name id; do
    [[ $name =~ $ACCOUNTS_REGEX ]] || continue
    if [ "$id" = "$via_acct" ] || [ "$id" = "$mgmt_acct" ]; then
      echo "  (skipping $name: it is the management/login account)"; continue
    fi
    printf '\n[profile teardown-%s]\nsso_session = %s\nsso_account_id = %s\nsso_role_name = %s\nregion = us-east-1\n' \
      "$name" "$session" "$id" "$ROLE" >>"$GEN_CONFIG"
    PROFILES+=("teardown-$name"); echo "  $name ($id)"
  done <<<"$accts"
  export AWS_CONFIG_FILE="$GEN_CONFIG"
fi
[ ${#PROFILES[@]} -gt 0 ] || { echo "no profiles/accounts selected" >&2; usage 1; }

JQ_DEFS='def nm: ((.Tags // []) | map(select(.Key=="Name")) | (.[0].Value // ""));
         def kept: ((.Tags // []) | map(select(.Key==$k and ((.Value // "")|ascii_downcase)=="true")) | length) > 0;'

# ---- helpers (R = region, D = per-region output dir, PROFILE = current profile) ----
q() { aws --profile "$PROFILE" --region "$R" "$@" 2>>"$D/err" </dev/null; }
emit_line() { local f=$1; shift; { printf 'run'; printf ' %q' "$@"; printf '\n'; } >>"$f"; }
e() { local p=$1; shift; emit_line "$D/phase.$p" aws --profile "$PROFILE" --region "$R" "$@"; }
note() { printf '%s\t%s\t%s\t%s\n' "${NOTE_REGION:-$R}" "$1" "$2" "$3" >>"$D/inv"; }
is_protected() { [ -n "$PROTECT_REGEX" ] && [[ ${1##*[/:]} =~ $PROTECT_REGEX ]]; }
is_kept_vpc() { [ -n "$1" ] && [[ $KEEP_VPCS == *" $1 "* ]]; }
names() { tr '\t' '\n' | while read -r n; do
            [ -n "$n" ] && [ "$n" != None ] && ! is_protected "$n" && echo "$n"
          done; }

discover_region() {
  local defvpc id nm typ st arn a b c prot eng cls vpc cidr ip alloc gb perm egr hasin hasegr s u
  defvpc=$(q ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
  [ "$defvpc" = None ] && defvpc=""
  KEEP_VPCS=" $(q ec2 describe-vpcs --output json | jq -r --arg k "$KEEP_TAG" "$JQ_DEFS"'.Vpcs[] | select(kept) | .VpcId' | tr '\n' ' ') "

  # CloudFormation stacks (03/04) - top-level only; deleting them removes what they own
  q cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE ROLLBACK_COMPLETE \
      CREATE_FAILED UPDATE_ROLLBACK_COMPLETE DELETE_FAILED ROLLBACK_FAILED UPDATE_ROLLBACK_FAILED \
      IMPORT_COMPLETE IMPORT_ROLLBACK_COMPLETE \
      --query 'StackSummaries[?ParentId==`null`].StackName' --output text | names |
  while read -r s; do
    note cloudformation-stack "$s" ""
    e 03 cloudformation delete-stack --stack-name "$s"
    e 04 cloudformation wait stack-delete-complete --stack-name "$s"
  done

  # Auto Scaling groups (05) - must go first or they respawn instances
  q autoscaling describe-auto-scaling-groups --query 'AutoScalingGroups[].AutoScalingGroupName' --output text | names |
  while read -r s; do
    note autoscaling-group "$s" ""
    e 05 autoscaling delete-auto-scaling-group --auto-scaling-group-name "$s" --force-delete
  done

  # EKS (06-09)
  q eks list-clusters --query 'clusters[]' --output text | names |
  while read -r c; do
    note eks-cluster "$c" ""
    q eks list-nodegroups --cluster-name "$c" --query 'nodegroups[]' --output text | tr '\t' '\n' |
    while read -r a; do
      [ -z "$a" ] && continue
      e 06 eks delete-nodegroup --cluster-name "$c" --nodegroup-name "$a"
      e 07 eks wait nodegroup-deleted --cluster-name "$c" --nodegroup-name "$a"
    done
    e 08 eks delete-cluster --name "$c"
    e 09 eks wait cluster-deleted --name "$c"
  done

  # EC2 instances (10/11)
  q ec2 describe-instances --filters 'Name=instance-state-name,Values=pending,running,stopping,stopped' --output json |
    jq -r --arg k "$KEEP_TAG" "$JQ_DEFS"'.Reservations[].Instances[] | select(kept|not)
        | [.InstanceId, .InstanceType, .State.Name, nm] | @tsv' |
  while IFS=$'\t' read -r id typ st nm; do
    is_protected "$nm" && continue
    note ec2-instance "$id" "$typ $st $nm"
    e 10 ec2 terminate-instances --instance-ids "$id"
    e 11 ec2 wait instance-terminated --instance-ids "$id"
  done

  # Load balancers (14-17)
  q elbv2 describe-load-balancers --query 'LoadBalancers[].[LoadBalancerArn,Type,LoadBalancerName]' --output text |
  while IFS=$'\t' read -r arn typ nm; do
    [ -z "$arn" ] && continue; is_protected "$nm" && continue
    note "load-balancer-$typ" "$nm" "$arn"
    e 14 elbv2 modify-load-balancer-attributes --load-balancer-arn "$arn" --attributes Key=deletion_protection.enabled,Value=false
    e 15 elbv2 delete-load-balancer --load-balancer-arn "$arn"
    e 16 elbv2 wait load-balancers-deleted --load-balancer-arns "$arn"
  done
  q elb describe-load-balancers --query 'LoadBalancerDescriptions[].LoadBalancerName' --output text | names |
  while read -r a; do
    note load-balancer-classic "$a" ""
    e 15 elb delete-load-balancer --load-balancer-name "$a"
  done
  q elbv2 describe-target-groups --query 'TargetGroups[].[TargetGroupArn,TargetGroupName]' --output text |
  while IFS=$'\t' read -r arn nm; do
    [ -z "$arn" ] && continue; is_protected "$nm" && continue
    note target-group "$nm" "$arn"
    e 17 elbv2 delete-target-group --target-group-arn "$arn"
  done

  # RDS (19-26)
  q rds describe-db-instances --query 'DBInstances[].[DBInstanceIdentifier,DeletionProtection,Engine,DBInstanceClass]' --output text |
  while IFS=$'\t' read -r id prot eng cls; do
    [ -z "$id" ] && continue; is_protected "$id" && continue
    note rds-instance "$id" "$eng $cls"
    [ "$prot" = True ] && e 19 rds modify-db-instance --db-instance-identifier "$id" --no-deletion-protection --apply-immediately
    e 20 rds delete-db-instance --db-instance-identifier "$id" --skip-final-snapshot --delete-automated-backups
    e 21 rds wait db-instance-deleted --db-instance-identifier "$id"
  done
  q rds describe-db-clusters --query 'DBClusters[].[DBClusterIdentifier,DeletionProtection,Engine]' --output text |
  while IFS=$'\t' read -r id prot eng; do
    [ -z "$id" ] && continue; is_protected "$id" && continue
    note rds-cluster "$id" "$eng"
    [ "$prot" = True ] && e 22 rds modify-db-cluster --db-cluster-identifier "$id" --no-deletion-protection --apply-immediately
    e 23 rds delete-db-cluster --db-cluster-identifier "$id" --skip-final-snapshot
    emit_line "$D/phase.24" wait_rds_cluster_gone "$PROFILE" "$R" "$id"
  done
  q rds describe-db-snapshots --snapshot-type manual --query 'DBSnapshots[].DBSnapshotIdentifier' --output text | names |
  while read -r a; do note rds-snapshot "$a" ""; e 25 rds delete-db-snapshot --db-snapshot-identifier "$a"; done
  q rds describe-db-cluster-snapshots --snapshot-type manual --query 'DBClusterSnapshots[].DBClusterSnapshotIdentifier' --output text | names |
  while read -r a; do note rds-cluster-snapshot "$a" ""; e 25 rds delete-db-cluster-snapshot --db-cluster-snapshot-identifier "$a"; done
  q rds describe-db-subnet-groups --query 'DBSubnetGroups[].DBSubnetGroupName' --output text | names | grep -v '^default' |
  while read -r a; do note rds-subnet-group "$a" ""; e 26 rds delete-db-subnet-group --db-subnet-group-name "$a"; done

  # NAT gateways + VPC endpoints (27-29)
  q ec2 describe-nat-gateways --filter Name=state,Values=pending,available --query 'NatGateways[].[NatGatewayId,VpcId]' --output text |
  while IFS=$'\t' read -r id vpc; do
    [ -z "$id" ] && continue; is_kept_vpc "$vpc" && continue
    note nat-gateway "$id" "$vpc"
    e 27 ec2 delete-nat-gateway --nat-gateway-id "$id"
    emit_line "$D/phase.28" wait_nat_gone "$PROFILE" "$R" "$id"
  done
  q ec2 describe-vpc-endpoints --filters Name=vpc-endpoint-state,Values=available,pending,pendingAcceptance \
      --query 'VpcEndpoints[].[VpcEndpointId,VpcId,ServiceName]' --output text |
  while IFS=$'\t' read -r id vpc nm; do
    [ -z "$id" ] && continue; is_kept_vpc "$vpc" && continue
    note vpc-endpoint "$id" "$nm"
    e 29 ec2 delete-vpc-endpoints --vpc-endpoint-ids "$id"
  done

  # Elastic IPs (31)
  q ec2 describe-addresses --query 'Addresses[].[AllocationId,PublicIp]' --output text |
  while IFS=$'\t' read -r alloc ip; do
    [ -z "$alloc" ] && continue; [ "$alloc" = None ] && continue
    note elastic-ip "$alloc" "$ip"
    e 31 ec2 release-address --allocation-id "$alloc"
  done

  # Serverless / messaging / registries (35-39)
  q lambda list-functions --query 'Functions[].FunctionName' --output text | names |
  while read -r a; do note lambda "$a" ""; e 35 lambda delete-function --function-name "$a"; done
  q dynamodb list-tables --query 'TableNames[]' --output text | names |
  while read -r a; do note dynamodb-table "$a" ""; e 36 dynamodb delete-table --table-name "$a"; done
  q sqs list-queues --query 'QueueUrls[]' --output text | names |
  while read -r u; do note sqs-queue "${u##*/}" "$u"; e 37 sqs delete-queue --queue-url "$u"; done
  q sns list-topics --query 'Topics[].TopicArn' --output text | names |
  while read -r a; do note sns-topic "${a##*:}" "$a"; e 38 sns delete-topic --topic-arn "$a"; done
  q ecr describe-repositories --query 'repositories[].repositoryName' --output text | names |
  while read -r a; do note ecr-repository "$a" ""; e 39 ecr delete-repository --repository-name "$a" --force; done

  # AMIs, snapshots, unattached volumes (41-43)
  q ec2 describe-images --owners self --query 'Images[].[ImageId,Name]' --output text |
  while IFS=$'\t' read -r id nm; do
    [ -z "$id" ] && continue; is_protected "$nm" && continue
    note ami "$id" "$nm"; e 41 ec2 deregister-image --image-id "$id"
  done
  q ec2 describe-snapshots --owner-ids self --query 'Snapshots[].[SnapshotId,VolumeSize]' --output text |
  while IFS=$'\t' read -r id gb; do
    [ -z "$id" ] && continue
    note ebs-snapshot "$id" "${gb}GB"; e 42 ec2 delete-snapshot --snapshot-id "$id"
  done
  q ec2 describe-volumes --filters Name=status,Values=available --output json |
    jq -r --arg k "$KEEP_TAG" "$JQ_DEFS"'.Volumes[] | select(kept|not) | [.VolumeId, .Size, nm] | @tsv' |
  while IFS=$'\t' read -r id gb nm; do
    is_protected "$nm" && continue
    note ebs-volume "$id" "${gb}GB $nm"; e 43 ec2 delete-volume --volume-id "$id"
  done

  # Networking (55-80): non-default VPCs and what lives in them
  q ec2 describe-network-interfaces --filters Name=status,Values=available --query 'NetworkInterfaces[].[NetworkInterfaceId,VpcId]' --output text |
  while IFS=$'\t' read -r id vpc; do
    [ -z "$id" ] && continue; is_kept_vpc "$vpc" && continue
    note network-interface "$id" "$vpc"; e 55 ec2 delete-network-interface --network-interface-id "$id"
  done
  q ec2 describe-security-groups --output json |
    jq -r '.SecurityGroups[] | select(.GroupName != "default")
        | [.GroupId, .VpcId, (.IpPermissions|length > 0), (.IpPermissionsEgress|any(.UserIdGroupPairs|length > 0)),
           (.IpPermissions|tojson), (.IpPermissionsEgress|tojson), .GroupName] | @tsv' |
  while IFS=$'\t' read -r id vpc hasin hasegr perm egr nm; do
    is_kept_vpc "$vpc" && continue; is_protected "$nm" && continue
    note security-group "$id" "$nm"
    # revoke rules first so groups that reference each other can be deleted
    [ "$hasin" = true ] && e 60 ec2 revoke-security-group-ingress --group-id "$id" --ip-permissions "$perm"
    [ "$hasegr" = true ] && e 60 ec2 revoke-security-group-egress --group-id "$id" --ip-permissions "$egr"
    e 76 ec2 delete-security-group --group-id "$id"
  done
  q ec2 describe-internet-gateways --query 'InternetGateways[].[InternetGatewayId,Attachments[0].VpcId]' --output text |
  while IFS=$'\t' read -r id vpc; do
    [ -z "$id" ] && continue
    [ "$vpc" = "$defvpc" ] && continue; is_kept_vpc "$vpc" && continue
    note internet-gateway "$id" "$vpc"
    [ "$vpc" != None ] && e 65 ec2 detach-internet-gateway --internet-gateway-id "$id" --vpc-id "$vpc"
    e 65 ec2 delete-internet-gateway --internet-gateway-id "$id"
  done
  q ec2 describe-subnets --query 'Subnets[?DefaultForAz==`false`].[SubnetId,VpcId,CidrBlock]' --output text |
  while IFS=$'\t' read -r id vpc cidr; do
    [ -z "$id" ] && continue; is_kept_vpc "$vpc" && continue
    note subnet "$id" "$vpc $cidr"; e 70 ec2 delete-subnet --subnet-id "$id"
  done
  q ec2 describe-route-tables --output json |
    jq -r '.RouteTables[] | select([.Associations[]?.Main] | any | not) | [.RouteTableId, .VpcId] | @tsv' |
  while IFS=$'\t' read -r id vpc; do
    is_kept_vpc "$vpc" && continue
    note route-table "$id" "$vpc"; e 72 ec2 delete-route-table --route-table-id "$id"
  done
  q ec2 describe-network-acls --query 'NetworkAcls[?IsDefault==`false`].[NetworkAclId,VpcId]' --output text |
  while IFS=$'\t' read -r id vpc; do
    [ -z "$id" ] && continue; is_kept_vpc "$vpc" && continue
    note network-acl "$id" "$vpc"; e 74 ec2 delete-network-acl --network-acl-id "$id"
  done
  q ec2 describe-vpcs --output json |
    jq -r --arg k "$KEEP_TAG" "$JQ_DEFS"'.Vpcs[] | select(.IsDefault|not) | select(kept|not) | [.VpcId, .CidrBlock, nm] | @tsv' |
  while IFS=$'\t' read -r id cidr nm; do
    is_protected "$nm" && continue
    note vpc "$id" "$cidr $nm"; e 80 ec2 delete-vpc --vpc-id "$id"
  done

  # Things this script will NOT delete but you should know about
  q ec2 describe-transit-gateways --query 'TransitGateways[?State!=`deleted`].TransitGatewayId' --output text | names |
  while read -r a; do note "MANUAL:transit-gateway" "$a" "not handled"; done
  q ec2 describe-vpc-peering-connections --filters Name=status-code,Values=active,pending-acceptance \
      --query 'VpcPeeringConnections[].VpcPeeringConnectionId' --output text | names |
  while read -r a; do note "MANUAL:vpc-peering" "$a" "not handled"; done
}

discover_global() {
  local b loc t
  NOTE_REGION=global
  # S3 (85)
  q s3api list-buckets --query 'Buckets[].Name' --output text | names |
  while read -r b; do
    t=$(q s3api get-bucket-tagging --bucket "$b" --query "TagSet[?Key=='$KEEP_TAG'].Value" --output text | tr 'A-Z' 'a-z')
    [ "$t" = true ] && continue
    note s3-bucket "$b" ""
    emit_line "$D/phase.85" empty_bucket "$PROFILE" "$b"
  done
  # IAM: report only
  q iam list-roles --output json |
    jq -r '.Roles[] | select(.Path|startswith("/aws-service-role/")|not) | select(.Path|startswith("/aws-reserved/")|not) | .RoleName' | names |
  while read -r b; do note "MANUAL:iam-role" "$b" "not deleted by this tool; review by hand"; done
  q iam list-users --query 'Users[].UserName' --output text | names |
  while read -r b; do note "MANUAL:iam-user" "$b" "not deleted by this tool; review by hand"; done
}

write_destroy_script() {
  local wd=$1 acct=$2 f=$1/destroy.sh n p
  {
    echo '#!/usr/bin/env bash'
    echo "# Generated $(date -u +%FT%TZ) by discover.sh. Review before running."
    echo '# Default is a DRY RUN. Pass --execute to actually delete (asks you to type the account ID,'
    echo '# or pass --confirm <account-id> to skip the prompt).'
    [ -n "$GEN_CONFIG" ] && printf 'export AWS_CONFIG_FILE=%q\n' "$GEN_CONFIG"
    printf 'PROFILE=%q\nACCOUNT=%q\n' "$PROFILE" "$acct"
    cat <<'HDR'
EXECUTE=0; CONFIRM=""
while [ $# -gt 0 ]; do
  case $1 in --execute) EXECUTE=1 ;; --confirm) CONFIRM=${2:-}; shift ;; esac
  shift
done
FAILED=0
run() {
  printf '+'; printf ' %q' "$@"; echo
  [ "$EXECUTE" = 1 ] || return 0
  "$@" || { echo "  ^ FAILED (continuing)" >&2; FAILED=$((FAILED+1)); }
}
wait_nat_gone() {  # profile region id
  local s _
  for _ in $(seq 1 60); do
    s=$(aws --profile "$1" --region "$2" ec2 describe-nat-gateways --nat-gateway-ids "$3" --query 'NatGateways[0].State' --output text 2>/dev/null)
    case $s in deleted|"") return 0 ;; esac
    sleep 10
  done
  return 1
}
wait_rds_cluster_gone() {  # profile region id
  local _
  for _ in $(seq 1 90); do
    aws --profile "$1" --region "$2" rds describe-db-clusters --db-cluster-identifier "$3" >/dev/null 2>&1 || return 0
    sleep 10
  done
  return 1
}
empty_bucket() {  # profile bucket - removes all versions + delete markers, then the bucket
  local p=$1 b=$2 region tmp n fails=0
  region=$(aws --profile "$p" s3api get-bucket-location --bucket "$b" --query LocationConstraint --output text 2>/dev/null)
  case $region in None|null|"") region=us-east-1 ;; EU) region=eu-west-1 ;; esac
  tmp=$(mktemp)
  while :; do
    aws --profile "$p" --region "$region" s3api list-object-versions --bucket "$b" --max-items 1000 --output json 2>/dev/null |
      jq '{Objects: ([.Versions[]?, .DeleteMarkers[]?] | map({Key, VersionId})), Quiet: true}' >"$tmp"
    n=$(jq '.Objects | length' "$tmp")
    [ "${n:-0}" -eq 0 ] && break
    aws --profile "$p" --region "$region" s3api delete-objects --bucket "$b" --delete "file://$tmp" >/dev/null || { fails=$((fails+1)); [ $fails -ge 3 ] && break; }
  done
  rm -f "$tmp"
  aws --profile "$p" --region "$region" s3api delete-bucket --bucket "$b"
}

actual=$(aws --profile "$PROFILE" sts get-caller-identity --query Account --output text) \
  || { echo "Cannot authenticate. Try: aws sso login --profile $PROFILE" >&2; exit 1; }
[ "$actual" = "$ACCOUNT" ] || { echo "Profile $PROFILE is account $actual, but this script was generated for $ACCOUNT. Aborting." >&2; exit 1; }
if [ "$EXECUTE" = 1 ]; then
  echo "About to DELETE every resource listed below in account $ACCOUNT (profile $PROFILE)."
  c=$CONFIRM
  [ -n "$c" ] || read -r -p "Type the account ID to continue: " c
  [ "$c" = "$ACCOUNT" ] || { echo "Aborted."; exit 1; }
else
  echo "DRY RUN - nothing will be changed. Re-run with --execute to delete."
fi
HDR
    for n in $(seq -w 0 99); do
      p=$(ls "$wd"/*/phase."$n" 2>/dev/null) || continue
      echo; echo "echo '=== phase $n ==='"
      cat "$wd"/*/phase."$n"
    done
    echo
    echo 'echo; echo "Done. $FAILED command(s) failed. Dependency errors are normal - wait a few minutes and re-run."'
    echo 'echo "Then re-run discover.sh to confirm the account is clean."'
  } >"$f"
  chmod +x "$f"
}

process_profile() {
  PROFILE=$1
  local acct wd r regions
  acct=$(aws --profile "$PROFILE" sts get-caller-identity --query Account --output text 2>/dev/null) || {
    echo "!! $PROFILE: cannot authenticate (try: aws sso login --profile $PROFILE) - skipped" >&2
    printf 'skipped\tcannot authenticate\n' >"$OUTDIR/.logs/$PROFILE.status"; return; }
  wd="$OUTDIR/$acct-$PROFILE"
  [ -n "$acct" ] && rm -rf "$wd"
  mkdir -p "$wd/global"
  echo "== $PROFILE (account $acct)"

  regions=$REGIONS
  [ -n "$regions" ] || regions=$(aws --profile "$PROFILE" ec2 describe-regions --query 'Regions[].RegionName' --output text --region us-east-1 2>/dev/null)
  [ -n "$regions" ] || { echo "!! $PROFILE: could not list regions" >&2
    printf 'skipped\tcould not list regions\n' >"$OUTDIR/.logs/$PROFILE.status"; return; }

  ( R=us-east-1; D="$wd/global"; discover_global ) &
  for r in $regions; do
    ( R=$r; D="$wd/region.$r"; mkdir -p "$D"; discover_region ) &
    while [ "$(jobs -rp | wc -l)" -ge "$MAXJOBS" ]; do sleep 0.3; done
  done
  wait

  { printf 'region\ttype\tid\tdetail\n'; cat "$wd"/*/inv 2>/dev/null | sort -t$'\t' -k1,1 -k2,2 -k3,3; } >"$wd/inventory.tsv"
  write_destroy_script "$wd" "$acct"

  local errs; errs=$(cat "$wd"/*/err 2>/dev/null | grep -c .)
  if [ "$(tail -n +2 "$wd/inventory.tsv" | wc -l)" -eq 0 ]; then
    echo "   nothing found"
  else
    echo "   Targeted by destroy.sh:"
    tail -n +2 "$wd/inventory.tsv" | grep -v $'\tMANUAL:' | cut -f2 | sort | uniq -c | sort -rn | sed 's/^/     /'
    if tail -n +2 "$wd/inventory.tsv" | grep -q $'\tMANUAL:'; then
      echo "   NOT handled (review by hand):"
      tail -n +2 "$wd/inventory.tsv" | grep $'\tMANUAL:' | cut -f2 | sort | uniq -c | sed 's/^/     /'
    fi
  fi
  [ "$errs" -gt 0 ] && echo "   $errs API error line(s) - some results may be missing; see $wd/region.*/err"
  echo "   inventory: $wd/inventory.tsv"
  echo "   destroy:   $wd/destroy.sh   (dry-run by default; --execute to delete)"
  printf 'ok\t%s\n' "$wd" >"$OUTDIR/.logs/$PROFILE.status"
}

write_summary() {
  local p st wd f
  for p in "${PROFILES[@]}"; do
    f="$OUTDIR/.logs/$p.status"; [ -f "$f" ] || continue
    IFS=$'\t' read -r st wd <"$f"
    if [ "$st" = ok ]; then
      tail -n +2 "$wd/inventory.tsv" | jq -Rn --arg profile "$p" --arg dir "$wd" '
        [inputs | split("\t") | {region: .[0], type: .[1], id: .[2], detail: (.[3] // "")}] as $r
        | {profile: $profile, account: ($dir | split("/") | last | split("-")[0]), status: "ok", dir: $dir,
           destroyable: ([$r[] | select(.type | startswith("MANUAL:") | not)] | length),
           manual: ([$r[] | select(.type | startswith("MANUAL:"))] | length), resources: $r}'
    else
      jq -n --arg profile "$p" --arg reason "$wd" '{profile: $profile, status: "skipped", reason: $reason}'
    fi
  done | jq -s '{accounts: .}' >"$OUTDIR/summary.json"
}

mkdir -p "$OUTDIR"; OUTDIR=$(cd "$OUTDIR" && pwd)
rm -rf "$OUTDIR/.logs"; mkdir -p "$OUTDIR/.logs"
for p in "${PROFILES[@]}"; do
  ( process_profile "$p" >"$OUTDIR/.logs/$p.log" 2>&1; echo "  scanned $p" >&2 ) &
  while [ "$(jobs -rp | wc -l)" -ge "$ACCOUNT_JOBS" ]; do sleep 0.3; done
done
wait
for p in "${PROFILES[@]}"; do cat "$OUTDIR/.logs/$p.log"; done
write_summary
