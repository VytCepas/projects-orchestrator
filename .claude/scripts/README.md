# `.claude/scripts/`

Helper scripts invoked by the user, by hooks, or by agents via the Bash tool. Prefer bash or python stdlib. Document each script with a one-line header comment.

## Available scripts

- **`install_hooks.sh`** — Symlink or copy git hooks from `.github/hooks/` to `.git/hooks/`
- **`create_issue.sh`** — Create a typed GitHub issue and print its issue number
- **`start_issue.sh`** — Create an issue branch, push it, and open a draft PR
- **`create_nojira_pr.sh`** — Create or reuse a no-issue branch, push it, and open a draft PR
- **`push_branch.sh`** — Push current branch with retry and remote-SHA verification (handles transient 5xx errors)
- **`promote_review.sh`** — Mark a draft PR ready for review
- **`monitor_pr.sh`** — Poll PR checks and optionally squash-merge with `--merge`
- **`finish_pr.sh`** — Composite: push, mark ready, monitor checks/review, and merge
- **`setup_github.sh`** — One-time GitHub repository governance setup (board fields, review settings; `--protect` applies baseline branch protection)
