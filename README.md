# sandbox-wipe

**Purpose:** after a class, students leave things running in the training AWS accounts
(named `sandbox01`, `sandbox02`, ... `sandbox19`). Those leftovers cost money. This tool finds
everything left behind in those accounts and deletes it, then checks again to prove the
accounts are empty.

**Jump to:** [I'm a person](#for-people-step-by-step) · [I'm an AI agent](#for-ai-agents) · [Reference](#reference)

---

## For people (step by step)

You do not need to know anything about AWS. You do need about an hour the first time
(mostly waiting) and permission to log in to the sandbox accounts.

### What it will and will not do

- It **looks** at every sandbox account and lists what is still running or stored.
- If you tell it to, it **permanently deletes** all of that. There is no undo.
- It will **never** touch the main/management AWS account or the account you log in with.
- It will **never** touch anything you have marked "keep" (see [Keeping something](#keeping-something)).

### One-time setup

You need a computer with a terminal (Linux, Mac, or Windows with WSL).

**1. Install two programs.**

| Program | What it is | Check if you have it | Install |
|---|---|---|---|
| `aws` (version 2) | AWS command line | `aws --version` | https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html |
| `jq` | reads data files | `jq --version` | Ubuntu/WSL: `sudo apt install jq` · Mac: `brew install jq` |

**2. Download this tool.**
```bash
git clone https://github.com/csfeeser/sandbox-wipe.git
cd sandbox-wipe
```
(The repo is private, so you must be signed in to GitHub with access to it.)

**3. Add the AWS login settings.** This tells the AWS tool where the company login page is.
Copy and paste this exactly, from inside the `sandbox-wipe` folder:
```bash
mkdir -p ~/.aws && cat aws-config.example >> ~/.aws/config
```
Do this **once**. If you run it twice, nothing breaks, but you will get duplicate lines.

**4. Make sure you have access.** Your AWS administrator must have given you the
`StudentSandboxCreator` role. If the login page in the next section does not show you
any accounts, ask them for access.

### Every time you want to clean up

**Step 1. Start the tool.**
```bash
cd sandbox-wipe
./sandbox-wipe
```

**Step 2. Choose "look first".** You will see:
```
=== sandbox-wipe ===
What do you want to do?
  1) Look at what is left in the sandboxes (safe - changes nothing)
  2) WIPE everything left in the sandboxes (deletes things permanently)
Choose 1 or 2 [1]:
```
Type `1` and press Enter. For every other question, just press Enter to accept the
suggestion in `[brackets]`. One question asks whether to use the **fast scan**: answer `y`
(it checks servers, disks, networks and storage, which is everything students can create
with their role; the full scan also tries databases, Lambda and similar, and is denied on them).

**Step 3. Log in if asked.** If you see `You are not logged in...`, a web browser opens.
Sign in, click **Allow**, then return to the terminal. (If no browser opens, the terminal
shows a web address and a short code: open the address on any device and type the code.)

**Step 4. Wait.** The scan takes about **8 minutes** with the fast scan (the default suggestion
in Step 2's questions) or about 16 minutes with the full scan. Leave the window open. You will see
`scanned sandbox05`, `scanned sandbox06`, ... as it goes.

**Step 5. Read the summary.** It ends like this:
```
---- Summary ----
  teardown-sandbox01: 15 to delete
  teardown-sandbox02: 0 to delete
  ...
  teardown-sandbox20: SKIPPED (cannot authenticate)
  TOTAL to delete: 75
```
- `0 to delete` means that account is already clean.
- `SKIPPED` means you do not have access to that account. That is normal for accounts
  that were never used.
- `TOTAL to delete: 0` means you are finished. Nothing to do.

**Step 6. Delete it all (only if the total is not 0).** Run the tool again:
```bash
./sandbox-wipe
```
Type `2` this time. It scans again, shows the numbers, and then asks:
```
!! This will PERMANENTLY DELETE the 75 resources above ...
Type WIPE (all capitals) to continue, anything else cancels:
```
Type `WIPE` and press Enter. Typing anything else cancels with nothing deleted.

**Step 7. Wait again (roughly 20 to 45 minutes).** It deletes everything, waits a short while for AWS to
finish, scans again, and repeats once if anything is left. It ends with one of:

- `Done. The sandboxes are clean.` : success.
- `Some resources remain...` : wait ten minutes and run `./sandbox-wipe` again. Some
  things can only be deleted after other things finish deleting.

### If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `'aws' is not installed` or `'jq' is not installed` | A program from setup step 1 is missing | Install it, then run again |
| `You are not logged in...` then login fails | Browser login did not complete | Run `aws sso login --profile sandbox-creator` and finish the browser steps |
| `profile sandbox-creator ... could not be found` | Setup step 3 was skipped | Do setup step 3 |
| Every account says `SKIPPED` | You can log in, but have no access to the sandbox accounts | Ask your AWS administrator for access |
| `Some resources remain` after two tries | AWS is still deleting something | Wait 10 minutes, run again. If it persists, send the `out/summary.json` file to an administrator |
| Wiped by mistake | | There is no undo. Deleted AWS resources cannot be recovered by this tool |

### Keeping something

To protect a resource from being deleted, add a tag to it in AWS with the key `Keep` and the
value `true`. This works for servers (EC2 instances), disks (EBS volumes), networks (VPCs) and
storage buckets (S3). Do this **before** running the wipe.

---

## For AI agents

### Purpose and context (read this first)

This repository contains one command, `./sandbox-wipe`. Its job: **find and permanently delete
all AWS resources left over in the class sandbox accounts** (accounts whose *name* matches
`^sandbox[0-9]+$`, reached through AWS Organizations and AWS SSO), then verify they are gone.
The humans who run training classes use it because students do not clean up after themselves.

### Hard rules

1. **Deletion is permanent.** Run `wipe` only if the human has explicitly told you, in this
   conversation, to delete the leftover resources. Otherwise run `scan` and report.
2. **Never add `--yes` on your own initiative.** `--yes` is the confirmation. Without it, a
   non-interactive `wipe` safely exits `3` after listing what it would delete.
3. **Do not widen the target.** Do not change `--accounts-regex`, `--role`, or `--profile` to
   reach accounts the human did not name. The tool already refuses to touch the Organization
   management account and the account you logged in with.
4. **Never invent credentials or edit `~/.aws/config` yourself.** If login is needed, ask the
   human to run the login command (it requires a browser).
5. Always pass `--non-interactive` so a missing answer fails fast instead of hanging.

### Procedure

```
1. Run the scan:        ./sandbox-wipe scan --non-interactive --fast --details
2. Read the exit code:
     0  -> accounts are clean. Report "nothing to delete". STOP.
     2  -> not logged in. Ask the human to run:  aws sso login --profile sandbox-creator
           then retry step 1.
     4  -> resources exist. Report the per-account counts (see "Reading the output"). Go to 3.
     1  -> bad usage/missing tool (aws, jq). Read stderr, fix, retry.
3. Ask the human whether to delete. Show them the totals. Do not proceed without a clear yes.
4. If they said yes:    ./sandbox-wipe wipe --non-interactive --fast --yes
5. Read the exit code:
     0  -> clean and verified. Report success.
     4  -> some resources remain (usually dependency timing). Wait ~10 minutes and repeat step 4
           once. If still 4, report the remaining items from out/summary.json to the human.
     2  -> login expired mid-run. Ask the human to log in, then repeat step 4.
```

**Run time:** `scan --fast` takes ~8 minutes (full `scan` ~16); `wipe` takes roughly 20-60 minutes
depending on `--fast` and how much there is to delete. Run it in the background (or with a
timeout of at least 90 minutes) and wait for completion. Do not kill it early.

### Commands (all defaults already correct for the class accounts)

| Goal | Command |
|---|---|
| Read-only report with every resource listed (recommended) | `./sandbox-wipe scan --non-interactive --fast --details` |
| Same, full scan (also probes DBs, Lambda, load balancers; ~2x slower) | `./sandbox-wipe scan --non-interactive --details` |
| Same, plus machine-readable JSON on stdout | `./sandbox-wipe scan --non-interactive --json` |
| Delete everything, verify (human approved) | `./sandbox-wipe wipe --non-interactive --fast --yes` |
| Preview what a wipe would delete, without deleting | `./sandbox-wipe wipe --non-interactive` (exits `3`, deletes nothing) |
| Limit to some regions | add `--regions us-east-1,us-east-2` |
| Protect names from deletion | add `--protect '^name-regex'` |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. For `scan`: nothing left. For `wipe`: everything deleted and verified, or nothing to delete |
| `1` | Bad usage, missing `aws`/`jq`, or scan failure (read stderr) |
| `2` | Not logged in to AWS SSO. Human must run `aws sso login --profile sandbox-creator` |
| `3` | `wipe` needs confirmation: you did not pass `--yes` and there is no terminal. Nothing was deleted |
| `4` | Resources remain (scan found some, or wipe could not delete all of them) |

### Reading the output

After every run, `out/summary.json` (path is printed) contains:

```json
{
  "accounts": [
    {
      "profile": "teardown-sandbox01",
      "account": "166493338807",
      "status": "ok",
      "dir": "/abs/path/out/166493338807-teardown-sandbox01",
      "destroyable": 15,
      "manual": 0,
      "resources": [
        {"region": "us-east-1", "type": "ec2-instance", "id": "i-0abc...", "detail": "t3.micro running testy-web"}
      ]
    },
    {"profile": "teardown-sandbox20", "status": "skipped", "reason": "cannot authenticate"}
  ]
}
```

- `destroyable` = number of resources `wipe` would delete. Total them across `status == "ok"` accounts.
- `manual` = items only listed, never deleted (types starting `MANUAL:`: IAM roles/users, transit
  gateways, VPC peering). Mention them to the human; the tool will not remove them.
- `status: "skipped"` = the human's login has no access to that account. Usually benign (unused
  account). Report it, do not try to work around it.
- Per-account detail: `out/<account>-<profile>/inventory.tsv` (what was found),
  `destroy.sh` (the exact commands, dry-run by default), `execute.log` (output of the last wipe).

### What it can and cannot see (tell the human when relevant)

- `--fast` scans only servers, disks, networks (VPC) and S3. That equals everything the default
  student role can see; if the human gives you a broader `--role`, use the full scan instead.
- Only the scanned regions (default `us-east-1,us-east-2,us-west-1,us-west-2`).
- Only what the permission set can read. The default role (`TerraformStudentLabs`) was denied on
  RDS, load balancers, Lambda, DynamoDB, IAM, CloudFormation and others. For those services,
  "nothing found" means "could not look". A role with broader read access can be given via
  `--role`, but only use a different role if the human tells you to.
- An independent check is AWS Cost Explorer in the management account (grouped by linked
  account); ongoing spend there means something is still running.

---

## Reference

### All flags

| Flag | Meaning |
|---|---|
| `scan` / `wipe` | Command. Omit both (in a terminal) for interactive mode |
| `--via PROFILE` | SSO profile that can list the AWS Organization (default `sandbox-creator`) |
| `--accounts-regex RE` | Which accounts, by name (default `^sandbox[0-9]+$`) |
| `--role NAME` | SSO permission set used inside each account (default `TerraformStudentLabs`) |
| `--profile NAME` | Use an AWS profile directly (repeatable); skips name matching |
| `--fast` | Only scan servers, disks, networks (VPC) and S3 buckets: about 2x faster. Same as `--services ec2,s3` |
| `--services LIST` | Choose service groups: `cfn asg eks ec2 elb rds serverless s3 iam` (default: all) |
| `--regions a,b` | Regions to check |
| `--keep-tag KEY` | Resources tagged `KEY=true` are never touched (default `Keep`) |
| `--protect REGEX` | Resource names matching this are never touched |
| `--yes` | Skip the confirmation (required when non-interactive) |
| `--rounds N` | Delete/re-scan passes to clear dependency errors (default 2) |
| `--no-verify` | Skip the final re-scan after deleting |
| `--details` | List every resource, not just counts |
| `--json` | Print the JSON summary at the end |
| `--out DIR` | Where reports go (default `./out`) |
| `--jobs N` | Accounts handled in parallel (default 4) |
| `--non-interactive` | Never prompt (implied when there is no terminal) |

Defaults can also be set in `wipe.conf` (copy `wipe.conf.example`), `~/.config/sandbox-wipe.conf`,
or environment variables `WIPE_VIA`, `WIPE_ACCOUNTS_REGEX`, `WIPE_ROLE`, `WIPE_REGIONS`,
`WIPE_PROTECT_REGEX`, `WIPE_KEEP_TAG`, `WIPE_OUT`.

### What gets deleted

EC2 instances, Auto Scaling groups, EKS clusters, load balancers and target groups, RDS/Aurora
(no final snapshot), NAT gateways, Elastic IPs, VPC endpoints, Lambda functions, DynamoDB tables,
SQS queues, SNS topics, ECR repositories, CloudFormation stacks, AMIs, EBS snapshots, unattached
EBS volumes, S3 buckets (including all object versions), and non-default VPCs with their subnets,
route tables, internet gateways and security groups.

**Never touched:** default VPCs; anything tagged `Keep=true` (instances, volumes, VPCs, buckets);
names matching the protect regex (Control Tower, SSO and CDK bootstrap names by default).
**Listed but not deleted:** IAM roles and users, transit gateways, VPC peering connections.

### How it works

`sandbox-wipe` calls `lib/discover.sh`, which lists accounts from AWS Organizations, keeps the
ones whose name matches the regex, creates temporary SSO profiles for them (in `out/aws-config`;
your `~/.aws/config` is never modified), and inventories each account and region. For each
account it writes `inventory.tsv` and a dependency-ordered `destroy.sh` that is a dry run unless
given `--execute`, and refuses to run unless the logged-in account ID matches the one it was
generated for. `wipe` runs those scripts with `--execute --confirm <account-id>`, waits, scans
again, and repeats up to `--rounds` times.

### Tests

```bash
./tests/smoke.sh     # offline; uses a mocked `aws`; needs no AWS account
```
