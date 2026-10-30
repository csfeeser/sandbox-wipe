# Instructions for AI agents

This project is one tool, `./sandbox-wipe`, that finds and **permanently deletes** leftover AWS
resources in the class sandbox accounts (accounts named `sandbox01`, `sandbox02`, ...).

Read the section **"For AI agents"** in `README.md` before running anything. The essentials:

1. Start with the read-only scan: `./sandbox-wipe scan --non-interactive --fast --details`.
2. Optionally preview a wipe: `./sandbox-wipe wipe --non-interactive --fast --dry-run` (deletes nothing).
3. Only run `./sandbox-wipe wipe --non-interactive --fast --yes` after the human has explicitly told
   you, in this conversation, to delete. Never add `--yes` on your own.
4. Always pass `--non-interactive`. Do not change `--accounts-regex`, `--role` or `--profile`
   to reach other accounts.
5. Exit codes: `0` clean/success, `1` usage/setup error, `2` not logged in (ask the human to run
   `aws sso login --profile sandbox-creator`), `3` confirmation required (nothing deleted),
   `4` resources remain.
6. A scan takes ~30 s; a wipe takes as long as AWS needs (minutes). Use a long timeout.
7. Results: `out/summary.json` (schema in the README). Report `coverage_gaps` and `errors` to the human.
