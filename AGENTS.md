# Instructions for AI agents

This repo is one tool, `./sandbox-wipe`, that finds and **permanently deletes** leftover AWS
resources in the class sandbox accounts (accounts named `sandbox01`, `sandbox02`, ...).

Read the section **"For AI agents"** in `README.md` before running anything. The essentials:

1. Start with the read-only scan: `./sandbox-wipe scan --non-interactive --fast --details`.
2. Only run `./sandbox-wipe wipe --non-interactive --fast --yes` after the human has explicitly told
   you, in this conversation, to delete. Never add `--yes` on your own.
3. Always pass `--non-interactive`. Do not change `--accounts-regex`, `--role` or `--profile`
   to reach other accounts.
4. Exit codes: `0` clean/success, `1` usage error, `2` not logged in (ask the human to run
   `aws sso login --profile sandbox-creator`), `3` confirmation required (nothing deleted),
   `4` resources remain.
5. `--fast` scans take ~8 min (full ~16); wipes ~20-60 min. Run in the background or with a long timeout.
6. Results: `out/summary.json` (schema in the README).
