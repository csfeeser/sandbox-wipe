# sandbox-wipe

Finds and deletes everything students leave running in the class sandbox AWS accounts
(`sandbox01`, `sandbox02`, ...). It works from a terminal with questions and plain-English
prompts, or fully from flags so a script or an AI agent can run it.

## Quick start (humans)

Needs `aws` (AWS CLI v2) and `jq` installed.

```bash
git clone <this repo> && cd sandbox-wipe
./sandbox-wipe
```

It will ask what you want to do:

1. **Look at what is left** - read-only, deletes nothing. Do this first.
2. **Wipe everything** - deletes it all, then re-scans to prove the accounts are clean.

If you are not logged in it opens the AWS browser login for you. You will need an AWS
SSO profile that can list the AWS Organization (ask whoever set up the sandboxes which
profile name to use). Before deleting anything it shows exactly how many resources it will
delete per account and makes you type `WIPE`.

## Flags (scripts and AI agents)

Everything it asks interactively has a flag. With no terminal attached it never prompts.

```bash
# Read-only report (exit 0 = clean, 4 = resources remain)
./sandbox-wipe scan --via PROFILE --non-interactive --details

# Delete. --yes is REQUIRED when not run from a terminal; without it the tool lists what it
# would delete and exits 3 without touching anything.
./sandbox-wipe wipe --via PROFILE --non-interactive --yes
```

| Flag | Meaning |
|---|---|
| `--via PROFILE` | SSO profile that can list the AWS Organization. Accounts are found by name. |
| `--accounts-regex RE` | Which accounts, by name (default `^sandbox[0-9]+$`) |
| `--role NAME` | SSO permission set to use inside each account (default `TerraformStudentLabs`) |
| `--profile NAME` | Scan an AWS profile directly (repeatable); skips name matching |
| `--regions a,b` | Regions to check (default `us-east-1,us-east-2,us-west-1,us-west-2`) |
| `--keep-tag KEY` | Resources tagged `KEY=true` are never touched (default `Keep`) |
| `--protect REGEX` | Resource names matching this are never touched |
| `--yes` | Skip the confirmation (required when non-interactive) |
| `--rounds N` | Delete/re-scan passes to clear dependency errors (default 2) |
| `--no-verify` | Skip the final re-scan |
| `--details` | List every resource, not only counts |
| `--json` | Also print the machine-readable summary |
| `--out DIR` | Report directory (default `./out`) |

Defaults can live in `wipe.conf` (copy `wipe.conf.example`) or `~/.config/sandbox-wipe.conf`.

**Exit codes:** `0` ok/clean, `1` bad usage, `2` not logged in (run `aws sso login --profile X`),
`3` confirmation required, `4` resources remain.

**Machine-readable output:** `out/summary.json` has, per account, `status`, `destroyable`,
`manual` and a `resources[]` list (`region`, `type`, `id`, `detail`). Per-account
`out/<account>-<profile>/destroy.sh` is the exact command list that will run.

### Notes for AI agents

- Run `scan` first and report the result to the human; run `wipe` only when they have said to
  delete. Deletion is permanent.
- Never pass `--yes` unless the human explicitly asked for the deletion in this session.
- The tool refuses to touch the Organization management account or the account you logged
  in with, whatever the regex says.
- If it exits `2`, ask the human to run the printed `aws sso login` command (it needs a browser).

## What gets deleted

EC2 instances, Auto Scaling groups, EKS clusters, load balancers and target groups, RDS/Aurora
(no final snapshot), NAT gateways, Elastic IPs, VPC endpoints, Lambda, DynamoDB, SQS, SNS, ECR,
CloudFormation stacks, AMIs, EBS snapshots and unattached volumes, S3 buckets (including all
versions), and non-default VPCs with their subnets, route tables, gateways and security groups.

Never touched: default VPCs, anything tagged `Keep=true` (instances, volumes, VPCs, buckets),
names matching the protect regex (Control Tower, SSO and CDK bootstrap names by default).
Listed but **not** deleted (review by hand): IAM roles and users, transit gateways, VPC peering.

## Limits you should know

- It can only see what the permission set allows. The student role was denied on RDS, load
  balancers, Lambda, IAM and others when this was written, so "nothing found" for those means
  "could not look". Use `--role` with a more powerful permission set if you need that coverage.
- Only the listed regions are scanned. Resources in other regions are invisible to it.
- Billing data (Cost Explorer in the management account) is a good independent cross-check.

## Tests

```bash
./tests/smoke.sh     # offline, uses a mocked `aws`, needs no AWS account
```

## How it works

`sandbox-wipe` wraps `lib/discover.sh`, which inventories each account and writes a
`destroy.sh` per account (dependency-ordered, dry-run by default, guarded by an account-ID check).
`wipe` scans, asks for confirmation, runs those scripts with `--execute --confirm <account-id>`,
then re-scans (up to `--rounds` times) and exits `4` if anything is left.
