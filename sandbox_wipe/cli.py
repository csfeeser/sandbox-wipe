"""Command line: interactive menu or flags. Exit codes: 0 ok/clean | 1 usage | 2 not logged in | 3 confirmation required | 4 resources remain."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import accounts as acct_mod
from .model import ALL_SERVICES, FAST_SERVICES, AccountResult
from .scan import scan_account
from .wipe import wipe_account

DEFAULT_VIA = "sandbox-creator"
DEFAULT_REGEX = r"^sandbox[0-9]+$"
DEFAULT_ROLE = "TerraformStudentLabs"
DEFAULT_REGIONS = "us-east-1,us-east-2,us-west-1,us-west-2"


def load_conf():
    """wipe.conf / ~/.config/sandbox-wipe.conf: simple KEY=value lines (same format as the bash version)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(here, "wipe.conf"), os.path.expanduser("~/.config/sandbox-wipe.conf")):
        if os.path.isfile(path):
            for line in open(path):
                m = re.match(r"\s*(?:export\s+)?(WIPE_\w+)=(.*?)\s*$", line)
                if m and not line.lstrip().startswith("#"):
                    os.environ.setdefault(m.group(1), m.group(2).strip("'\""))


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def build_parser(d):
    p = argparse.ArgumentParser(prog="sandbox-wipe", add_help=False)
    p.add_argument("command", nargs="?", choices=["scan", "wipe"])
    p.add_argument("--via", default=d["via"])
    p.add_argument("--accounts-regex", default=d["regex"])
    p.add_argument("--role", default=d["role"])
    p.add_argument("--profile", action="append", default=[])
    p.add_argument("--regions", default=d["regions"])
    p.add_argument("--keep-tag", default=d["keep"])
    p.add_argument("--protect", default=d["protect"])
    p.add_argument("--fast", action="store_true")
    p.add_argument("--services", default=d["services"])
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--details", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", default=d["out"])
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--threads", type=int, default=16)
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    return p


HELP = """sandbox-wipe - delete everything left behind in the class sandbox AWS accounts

USAGE
  ./sandbox-wipe                      interactive mode (asks questions, explains as it goes)
  ./sandbox-wipe scan [options]       read-only report of what is left
  ./sandbox-wipe wipe [options]       delete it all, then re-scan to confirm

WHICH ACCOUNTS
  --via PROFILE         AWS SSO profile that can list the AWS Organization (default: {via})
  --accounts-regex RE   match accounts by NAME (default: '{regex}')
  --role NAME           permission set to use inside each account (default: {role})
  --profile NAME        scan this AWS profile directly instead (repeatable; skips name matching)

WHAT TO SCAN
  --regions R1,R2       regions (default: {regions})
  --fast                only servers, disks, networks (VPC) and S3 (same as --services ec2,s3)
  --services LIST       service groups: {services} (default: all)
  --keep-tag KEY        resources tagged KEY=true are never touched (default: {keep})
  --protect REGEX       resource names matching this are never touched

BEHAVIOUR
  --yes                 wipe: do not ask for confirmation (REQUIRED when not run from a terminal)
  --dry-run             wipe: print the exact steps in order, delete nothing
  --no-verify           wipe: skip the final re-scan
  --rounds N            wipe: delete/re-scan passes to clear dependency errors (default 2)
  --details             list every resource, not just counts
  --json                print the machine-readable summary (out/summary.json) when done
  --out DIR             where reports go (default: {out})
  --jobs N              accounts processed in parallel (default 4)
  --threads N           parallel API calls per account (default 16)
  --non-interactive     never prompt (implied when there is no terminal)

Exit codes: 0 ok/clean | 1 bad usage | 2 not logged in | 3 confirmation required | 4 resources remain
"""


def ask(question, default=""):
    ans = input(f"{question}{f' [{default}]' if default else ''}: ").strip()
    return ans or default


def can_auth(profile):
    try:
        session_for(profile).client("sts").get_caller_identity()
        return True
    except (BotoCoreError, ClientError):
        return False


def ensure_login(profiles, interactive):
    known = set(boto3.Session().available_profiles)
    for p in profiles:
        if p != "default" and p not in known:
            die(f"AWS profile '{p}' was not found in your AWS config. Add it first (see the README, 'One-time setup', "
                f"step 3), or pick another with --via / --profile.", 1)
        if can_auth(p):
            continue
        if not interactive:
            die(f"not logged in to '{p}'. Run: aws sso login --profile {p}", 2)
        if not shutil.which("aws"):
            die(f"not logged in to '{p}', and the 'aws' command (needed only for logging in) is not installed", 2)
        print(f"You are not logged in to '{p}'. Opening the AWS login (follow the browser prompt)...")
        if subprocess.call(["aws", "sso", "login", "--profile", p]) != 0 or not can_auth(p):
            die(f"login failed for '{p}'", 2)


def session_for(profile):
    return boto3.Session(profile_name=profile if profile != "default" else None)


def scan_all(args, services, say=print):
    """Scan every selected account. Returns [AccountResult] in a stable order and writes out/summary.json."""
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    regions = [r for r in re.split(r"[,\s]+", args.regions) if r]
    if args.profile:
        targets = [acct_mod.Target(p) for p in args.profile]
    else:
        targets = acct_mod.resolve(args.via, args.accounts_regex, args.role, out, say)
    if not targets:
        die("no accounts matched")
    say(f"Scanning {len(targets)} account(s), regions: {', '.join(regions)}, services: {'all' if set(services) == set(ALL_SERVICES) else ','.join(services)}")

    def one(t):
        try:
            session = session_for(t.profile)
            aid = session.client("sts").get_caller_identity()["Account"]
        except (BotoCoreError, ClientError):
            return AccountResult(t.profile, status="skipped", reason="cannot authenticate")
        res = scan_account(session, t.profile, aid, regions, set(services), args.keep_tag, args.protect, args.threads)
        d = os.path.join(out, f"{aid}-{t.profile}")
        os.makedirs(d, exist_ok=True)
        res.dir = d
        with open(os.path.join(d, "inventory.tsv"), "w") as f:
            f.write("region\ttype\tid\tdetail\n")
            for r in res.resources:
                f.write(f"{r.region}\t{r.type}\t{r.id}\t{r.detail}\n")
        with open(os.path.join(d, "errors.log"), "w") as f:
            f.write("\n".join(res.errors) + ("\n" if res.errors else ""))
        print(f"  scanned {t.profile}", file=sys.stderr)
        return res

    with ThreadPoolExecutor(max(args.jobs, 1)) as pool:
        results = list(pool.map(one, targets))
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump({"accounts": [r.to_json() for r in results]}, f, indent=2)
    return results


def total(results):
    return sum(r.destroyable for r in results if r.status == "ok")


def show_overview(results, say=print):
    say("\n---- Summary ----")
    for r in results:
        if r.status != "ok":
            say(f"  {r.profile}: SKIPPED ({r.reason})")
            continue
        extra = f", {r.manual} to review by hand (IAM/etc)" if r.manual else ""
        say(f"  {r.profile}: {r.destroyable} to delete{extra}")
        if r.gaps:
            say(f"      could not look at (no permission): {', '.join(r.gaps)}")
        if r.errors:
            say(f"      {len(r.errors)} unexpected API error(s) - results may be incomplete; see {r.dir}/errors.log")
    say(f"  TOTAL to delete: {total(results)}")


def show_details(results, say=print):
    rows = [(r.profile, x.region, x.type, x.id, x.detail) for r in results if r.status == "ok" for x in r.resources]
    if not rows:
        return
    w = [max(len(str(row[i])) for row in rows) for i in range(4)]
    for row in rows:
        say("  ".join(str(row[i]).ljust(w[i]) for i in range(4)) + "  " + str(row[4]))


def do_wipe_round(results, args, dry_run):
    def one(r):
        sess = session_for(r.profile)
        lines = []
        try:
            ok, fails = wipe_account(sess, r.account, r.resources, args.threads, dry_run, lines.append)
        except RuntimeError as e:
            lines.append(f"ABORTED: {e}")
            ok, fails = 0, [str(e)]
        with open(os.path.join(r.dir, "plan.log" if dry_run else "execute.log"), "a") as f:
            f.write(f"=== {time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n" + "\n".join(lines) + "\n")
        print(f"  finished {r.profile} ({ok} ok, {len(fails)} failed; log: {r.dir}/{'plan' if dry_run else 'execute'}.log)")
        return fails

    todo = [r for r in results if r.status == "ok" and r.destroyable]
    with ThreadPoolExecutor(max(args.jobs, 1)) as pool:
        return list(pool.map(one, todo))


def main(argv=None):
    load_conf()
    env = os.environ.get
    defaults = {"via": env("WIPE_VIA", DEFAULT_VIA), "regex": env("WIPE_ACCOUNTS_REGEX", DEFAULT_REGEX),
                "role": env("WIPE_ROLE", DEFAULT_ROLE), "regions": env("WIPE_REGIONS", DEFAULT_REGIONS),
                "keep": env("WIPE_KEEP_TAG", "Keep"), "protect": env("WIPE_PROTECT_REGEX") or None,
                "services": env("WIPE_SERVICES", ""), "out": env("WIPE_OUT", "./out")}
    parser = build_parser(defaults)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(HELP.format(**{**defaults, "services": " ".join(ALL_SERVICES)}), file=sys.stderr)
        return 1
    if args.help:
        print(HELP.format(**{**defaults, "services": " ".join(ALL_SERVICES)}))
        return 0

    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.non_interactive
    if args.fast:
        args.services = ",".join(FAST_SERVICES)
    fast_given = bool(args.services)

    if interactive:
        print("=== sandbox-wipe ===")
        if not args.command:
            print("What do you want to do?\n  1) Look at what is left in the sandboxes (safe - changes nothing)\n"
                  "  2) WIPE everything left in the sandboxes (deletes things permanently)")
            choice = ask("Choose 1 or 2", "1")
            if choice not in ("1", "2"):
                die("please answer 1 or 2")
            args.command = "scan" if choice == "1" else "wipe"
        if not args.profile and args.via == DEFAULT_VIA and not os.environ.get("WIPE_VIA"):
            print("\nI need an AWS login profile that is allowed to list the sandbox accounts.")
            print("Profiles on this machine:")
            for pn in boto3.Session().available_profiles:
                print(f"  - {pn}")
            args.via = ask("Which profile?", DEFAULT_VIA)
        if not args.profile:
            args.accounts_regex = ask("Match accounts whose NAME matches this pattern", args.accounts_regex)
            args.role = ask("Permission set to use inside each account", args.role)
        args.regions = ask("Regions to check (comma separated)", args.regions)
        if not fast_given:
            print("\nFast scan checks only servers, disks, networks and S3 storage (about 2x quicker or more).")
            print("Full scan also checks databases, load balancers, Lambda, etc. (the student role cannot create those).")
            if ask("Use the fast scan? (y/n)", "y" if args.role == DEFAULT_ROLE else "n").lower().startswith("y"):
                args.services = ",".join(FAST_SERVICES)

    if not args.command:
        print(HELP.format(**{**defaults, "services": " ".join(ALL_SERVICES)}), file=sys.stderr)
        die("choose a command: scan or wipe (or run with no arguments in a terminal)")

    services = tuple(s for s in re.split(r"[,\s]+", args.services) if s) or ALL_SERVICES
    bad = [s for s in services if s not in ALL_SERVICES]
    if bad:
        die(f"unknown service group '{bad[0]}' (valid: {' '.join(ALL_SERVICES)})")
    need_yes = args.command == "wipe" and not interactive and not args.yes and not args.dry_run

    ensure_login(args.profile or [args.via], interactive)

    def finish(results):
        if args.json:
            print(open(os.path.join(os.path.abspath(args.out), "summary.json")).read())

    summary_path = os.path.join(os.path.abspath(args.out), "summary.json")
    if args.command == "scan":
        results = scan_all(args, services)
        show_overview(results)
        if args.details:
            print()
            show_details(results)
        print(f"\nFull report: {summary_path}")
        finish(results)
        return 0 if total(results) == 0 else 4

    # ---- wipe
    confirmed = False
    for rnd in range(1, args.rounds + 1):
        print(f"\n##### Scan, pass {rnd} of {args.rounds}")
        results = scan_all(args, services)
        n = total(results)
        if n == 0:
            print("\nNothing left to delete. The sandboxes are clean.")
            if any(r.manual for r in results):
                print(f"(Some items need review by hand - see {summary_path}, entries typed MANUAL:*)")
            finish(results)
            return 0
        show_overview(results)
        if args.dry_run:
            print("\n##### Dry run: steps in order (nothing is deleted)")
            do_wipe_round(results, args, True)
            for r in results:
                if r.status == "ok" and r.destroyable:
                    print(f"\n--- {r.profile}")
                    print(open(os.path.join(r.dir, "plan.log")).read().split("\n", 1)[1])
            finish(results)
            return 0
        if not confirmed:
            if args.details or need_yes:
                print()
                show_details(results)
            if need_yes:
                print("\nRefusing to delete without confirmation. Re-run with --yes to proceed.", file=sys.stderr)
                finish(results)
                return 3
            if not args.yes:
                print(f"\n!! This will PERMANENTLY DELETE the {n} resources above (instances, networks, S3 buckets and their contents).")
                print(f"!! Anything tagged {args.keep_tag}=true is left alone. This cannot be undone.")
                print("   (Run './sandbox-wipe scan --details' first if you want to see the full list.)")
                if input("Type WIPE (all capitals) to continue, anything else cancels: ").strip() != "WIPE":
                    print("Cancelled. Nothing was deleted.")
                    return 0
            confirmed = True
        print(f"\n##### Deleting, pass {rnd}")
        do_wipe_round(results, args, False)
        if args.no_verify:
            print("Skipped verification (--no-verify).")
            finish(results)
            return 0
        if rnd < args.rounds:
            wait = int(os.environ.get("SANDBOX_WIPE_ROUND_WAIT", "30"))
            print(f"Waiting {wait}s for AWS to finish deleting dependencies...")
            time.sleep(wait)

    print("\n##### Final verification scan")
    results = scan_all(args, services)
    if total(results) == 0:
        print("\nDone. The sandboxes are clean.")
        finish(results)
        return 0
    show_overview(results)
    print()
    show_details(results)
    print("\nSome resources remain. Wait a few minutes and run './sandbox-wipe wipe' again; see execute.log files under "
          f"{os.path.abspath(args.out)} for errors.", file=sys.stderr)
    finish(results)
    return 4
