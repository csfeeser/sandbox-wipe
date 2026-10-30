#!/usr/bin/env bash
# Offline smoke test: runs sandbox-wipe against a mocked `aws` (no network, no AWS account needed).
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(dirname "$HERE")
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"; cp "$HERE/mock-aws" "$T/bin/aws"; chmod +x "$T/bin/aws"
export PATH="$T/bin:$PATH" AWS_CONFIG_FILE="$T/config"; : >"$T/config"
fail=0
check() { if eval "$2"; then echo "ok   - $1"; else echo "FAIL - $1"; fail=1; fi; }
W="$ROOT/sandbox-wipe"

# 1. name matching picks sandboxNN only, never the management/login account
out=$("$W" scan --via mock-via --non-interactive --out "$T/o1" --regions us-east-1,us-west-2 2>&1); rc=$?
check "scan finds resources (exit 4 = something left)" '[ $rc -eq 4 ]'
check "matches sandbox01 and sandbox02" 'grep -q teardown-sandbox01 <<<"$out" && grep -q teardown-sandbox02 <<<"$out"'
check "ignores non-matching names" '! grep -q sandbox99 <<<"$out" && ! grep -q other-account <<<"$out"'
check "summary.json written" 'jq -e ".accounts|length==2" "$T/o1/summary.json" >/dev/null'
check "protected/kept resources not targeted" '! grep -q i-keep "$T"/o1/*/destroy.sh && ! grep -q vpc-keep "$T"/o1/*/destroy.sh'

# 2. wipe without --yes in non-interactive mode must refuse and delete nothing
out=$("$W" wipe --via mock-via --non-interactive --out "$T/o2" --regions us-east-1 2>&1); rc=$?
check "wipe refuses without --yes (exit 3)" '[ $rc -eq 3 ]'
check "no execute.log created when refused" '! ls "$T"/o2/*/execute.log >/dev/null 2>&1'

# 3. wipe with --yes executes
out=$("$W" wipe --via mock-via --non-interactive --yes --no-verify --out "$T/o3" --regions us-east-1 2>&1); rc=$?
check "wipe --yes --no-verify exits 0" '[ $rc -eq 0 ]'
check "destroy scripts executed" 'grep -q "terminate-instances" "$T"/o3/*/execute.log'

# 4. direct profile mode + generated destroy.sh is dry-run by default
"$W" scan --profile teardown-sandbox01 --non-interactive --out "$T/o4" --regions us-east-1 >/dev/null 2>&1
d=$(ls -d "$T"/o4/*-teardown-sandbox01)
dry=$("$d/destroy.sh" 2>&1)
check "destroy.sh dry-run by default" 'grep -q "DRY RUN" <<<"$dry"'
check "destroy.sh refuses wrong confirmation" '! "$d/destroy.sh" --execute --confirm 000000000000 >/dev/null 2>&1'

# 5. --fast / --services only scan the chosen groups
"$W" scan --via mock-via --non-interactive --fast --out "$T/o5" --regions us-east-1,us-west-2 >/dev/null 2>&1
types5=$(jq -r '.accounts[].resources[].type' "$T/o5/summary.json" | sort -u | tr '\n' ' ')
check "--fast still finds ec2 + s3 resources" 'grep -q ec2-instance <<<"$types5" && grep -q s3-bucket <<<"$types5" && grep -q vpc <<<"$types5"'
check "--fast skips lambda/rds/cloudformation" '! grep -qE "lambda|rds|cloudformation" <<<"$types5"'
"$W" scan --via mock-via --non-interactive --services s3 --out "$T/o6" --regions us-east-1 >/dev/null 2>&1
types6=$(jq -r '.accounts[].resources[].type' "$T/o6/summary.json" | sort -u | tr '\n' ' ')
check "--services s3 finds only buckets" '[ "$types6" = "s3-bucket " ]'
out=$("$W" scan --via mock-via --non-interactive --services bogus --out "$T/o7" 2>&1); rc=$?
check "unknown service group is rejected" '[ $rc -ne 0 ] && grep -q "unknown service group" <<<"$out"'

# 6. fail closed: if the VPC lookup fails, never emit network deletions (could hit default/kept VPCs)
MOCK_FAIL_VPCS=1 "$W" scan --via mock-via --non-interactive --out "$T/o8" --regions us-east-1 >/dev/null 2>&1
check "VPC lookup failure => no network deletes generated" '! grep -qE "delete-vpc|delete-subnet|internet-gateway|delete-nat|delete-security-group" "$T"/o8/*/destroy.sh'
check "VPC lookup failure => instances still handled" 'grep -q terminate-instances "$T"/o8/*/destroy.sh'

[ $fail -eq 0 ] && echo "ALL PASSED" || { echo "SOME TESTS FAILED"; exit 1; }
