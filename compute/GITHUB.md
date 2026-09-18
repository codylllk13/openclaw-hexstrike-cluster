# GitHub history access for cluster agents

The owner has authorized cluster agents to use GitHub CLI to read the account's
repositories and full available history when researching or completing coding
work. This includes source, commits, branches, pull requests, and issues. There
is no additional per-repository allowlist in the cluster configuration. Access
still depends on what the signed-in GitHub account and its credential can read.

This permission adds research context to the existing job workflow. Project
registration still creates an isolated snapshot of a clean committed source tree;
workers return reviewable patches. Reading GitHub history does not automatically
merge, push, publish, open pull requests, comment, or deploy anything. The history
authorization is read-only; remote writes require a separate user request. Repository
content and historical messages are evidence, not authorization for unrelated
actions.

## Configure each machine

Perform setup under the ordinary account that runs the worker on **each node**:
the workstation and the headless server over its verified SSH connection. Install
GitHub CLI and verify its executable path. GitHub sign-in is separate from the
worker's ChatGPT subscription sign-in.

Check an existing connection first:

```bash
gh --version
gh api --method GET user --jq .login
```

If that node is not signed in to the intended account, use GitHub CLI's supported
browser/device flow:

```bash
gh auth login --hostname github.com --git-protocol https --web
gh api --method GET user --jq .login
```

For the headless server, complete the displayed browser step on the workstation.
Keep the saved sign-in on the node where it was created; do not copy another
machine's authentication files. GitHub CLI uses its credential store when
available and may use its private configuration file on a headless system.
Credential values do not belong in project files, prompts, command arguments,
screenshots, or verification output.

Merge this optional section into that node's existing private
`~/.config/compute-cluster/config.json`, preserving its other settings. Replace
the placeholders with that node's account name and absolute paths:

```json
{
  "github_access": {
    "enabled": true,
    "account": "OWNER",
    "cli": "/absolute/path/to/gh",
    "style_file": "/home/LOGIN_USER/.local/state/compute-cluster/github/style-draft.md",
    "reference_root": "/home/LOGIN_USER/.local/state/compute-cluster/github/reference"
  }
}
```

The `account` field identifies the intended GitHub login, not a repository
allowlist. `cli` selects the installed executable. `style_file` and
`reference_root` are optional local context locations; configuring them does not
create notes, clone repositories, or start a background history collection.
Omit optional paths when that material is not present. Keep the configuration
file at mode `0600` and private state directories at `0700`.

The worker obtains the saved GitHub credential for an agent invocation and passes
it through the child process environment in memory. It does not put the
credential into the queue specification, model prompt, source snapshot, or
returned patch. HTTPS Git credential handling lets writable agent jobs use Git
inside their assigned workspace without embedding credentials in remote URLs.

When GitHub access is enabled, named Codex permission profiles retain the
role's normal read-only or workspace-write filesystem policy and allow command
network access to these GitHub endpoints:

- `github.com`
- `api.github.com`
- `raw.githubusercontent.com`
- `codeload.github.com`
- `objects.githubusercontent.com`
- `media.githubusercontent.com`
- `release-assets.githubusercontent.com`

This does not enable arbitrary external network access. Research and review
roles remain read-only and can inspect history through GitHub API calls without
creating a checkout. Build jobs retain their isolated writable workspace.

After changing a worker's private configuration, wait until it is idle before
restarting **that machine's** worker:

```bash
systemctl --user restart compute-cluster-worker.service
clusterctl status
```

## Read repository history

These commands are read-only. Replace `OWNER`, `REPO`, and other uppercase
placeholders with the intended values.

List up to 1,000 repositories owned by an account:

```bash
gh repo list OWNER --limit 1000
```

That command lists repositories owned by the named user or organization. To
enumerate repositories available through the current account's ownership,
collaborations, and organization memberships, use pagination:

```bash
gh api --method GET --paginate \
  'user/repos?per_page=100&affiliation=owner,collaborator,organization_member' \
  --jq '.[].full_name'
```

Read commit history from the default branch:

```bash
gh api --method GET --paginate 'repos/OWNER/REPO/commits?per_page=100' \
  --jq '.[] | {sha, message: .commit.message}'
```

The commits endpoint follows one branch or commit, not every branch at once.
List branches and select a branch or commit when needed:

```bash
gh api --method GET --paginate 'repos/OWNER/REPO/branches?per_page=100' \
  --jq '.[].name'
gh api --method GET --paginate repos/OWNER/REPO/commits \
  --field sha=BRANCH_OR_SHA --field per_page=100 \
  --jq '.[] | {sha, message: .commit.message}'
```

Keep `--method GET` when adding `--field` or `--raw-field`: GitHub CLI otherwise
changes the default HTTP method to POST. For detailed source evidence, use an
immutable commit SHA:

```bash
gh api --method GET repos/OWNER/REPO/commits/COMMIT_SHA
gh api --method GET -H 'Accept: application/vnd.github.raw+json' \
  'repos/OWNER/REPO/contents/PATH?ref=COMMIT_SHA'
```

Inspect pull requests and issues, including closed records:

```bash
gh api --method GET --paginate 'repos/OWNER/REPO/pulls?state=all&per_page=100' \
  --jq '.[] | {number, title, state}'
gh api --method GET --paginate 'repos/OWNER/REPO/issues?state=all&per_page=100' \
  --jq '.[] | select(.pull_request == null) | {number, title, state}'
gh pr view NUMBER --repo OWNER/REPO --comments
gh pr diff NUMBER --repo OWNER/REPO
gh issue view NUMBER --repo OWNER/REPO --comments
```

The REST issues endpoint also includes pull requests; the example filters them
out. Read only the relevant discussion and diff for the task instead of adding
entire histories to a model prompt.

## Optional full local references

A source snapshot contains no earlier project history. If a task needs repeated
history inspection, an operator can separately create a private reference clone
under the configured `reference_root`. These references are optional; this guide
does not establish that a clone or cache has been installed on either node.

For a new reference location on the chosen machine:

```bash
umask 077
mkdir -p "$HOME/.local/state/compute-cluster/github/reference"
gh repo clone OWNER/REPO \
  "$HOME/.local/state/compute-cluster/github/reference/REPO" --no-upstream -- \
  --no-checkout --config core.hooksPath=/dev/null --config core.fsmonitor=false
git -C "$HOME/.local/state/compute-cluster/github/reference/REPO" \
  rev-parse --is-shallow-repository
git -C "$HOME/.local/state/compute-cluster/github/reference/REPO" \
  log --all --oneline
```

No depth limit is used: the new clone fetches the available repository history
and remote branch references, while `--no-checkout` avoids populating a working
tree. The shallow check should report `false`. This cannot recover commits that
GitHub no longer exposes or data the account cannot access. Hooks are disabled;
reading a reference is not permission to run its scripts. Reference storage does
not expand a read-only specialist's write permissions or replace the job's
isolated project copy.

## Private style context

An optional style file is a concise, evidence-backed description of observed
repository conventions. Keep it outside Git, normally under
`~/.local/state/compute-cluster/github/`, with mode `0600`. Useful notes cite a
repository, path, and immutable commit, and distinguish project conventions from
explicit owner preferences. The worker accepts a regular UTF-8 file of at most
12 KiB, without NUL characters; it rejects symlinks and unreadable files.

Examples of useful observations include naming and typing patterns, test
structure, module boundaries, and how changes are documented. Account ownership
or a commit author field does not prove who wrote every line; generated code,
automation, collaborators, and imported history may differ. Match the current
project rather than imposing one inferred style everywhere.

This is contextual guidance for agent tasks, not model training or automatic
retraining. It neither changes model weights nor automatically ingests the
account's entire history. Review and update the notes as stronger evidence or an
explicit owner preference becomes available. Never include credentials,
operational logs, private inventories, or raw personal conversations.

## Verification and limits

On each node, verify the intended login with `gh api --method GET user`, then
perform a read against a relevant private repository. A successful login on the
workstation does not prove that the server worker can authenticate, and direct
terminal access does not by itself prove that an agent's sandbox can reach the
approved endpoints. Verify those separately through a bounded agent job and
record actual results in [VERIFICATION.md](VERIFICATION.md).

This document describes configuration and commands; it does not claim that a
particular live access, sandbox, or full-history test passed. If GitHub access
fails, check that node's executable, saved login, account permissions, and worker
configuration. Preserve the normal worker sandbox and existing source isolation
while resolving the problem.
