# bb - Bitbucket CLI

<p align="center">
  <img src="docs/img/social-preview.png" alt="bb — Bitbucket Cloud CLI" width="720" />
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/daniel-pittman/bitbucket-cli/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/daniel-pittman/bitbucket-cli/actions/workflows/ci.yml)
[![Bash](https://img.shields.io/badge/bash-3.2%2B-1f425f.svg)](https://www.gnu.org/software/bash/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![GitHub release](https://img.shields.io/github/v/release/daniel-pittman/bitbucket-cli)](https://github.com/daniel-pittman/bitbucket-cli/releases)

A lightweight command-line interface for Bitbucket Cloud. Wraps the Bitbucket REST API for common operations like managing pipelines, pull requests, and repositories. Ships with a Python [MCP server](#mcp-server-for-claude-code--ai-agents) so any [Claude Code](https://docs.claude.com/en/docs/claude-code) session (or any other MCP-aware client) can drive Bitbucket Cloud as native tools.

The bash CLI has no dependencies beyond `curl` and `jq`. The MCP server adds Python 3.10+. Works on macOS, Linux, and WSL.

## Features

- **Pipelines**: List, view, watch, trigger, and stop pipeline builds
- **Pull Requests**: Create, view, approve, unapprove, merge, decline, diff, comment
- **Repositories**: List repos, view details, list/show branches, list recent commits
- **Browser Integration**: Quick-open any resource in your browser
- **MCP server**: 31 tools covering the full surface, plus git-context wrappers (current branch, status, recent commits, uncommitted changes) for agent workflows

## Requirements

- `bash` (3.2+) — works with macOS system bash; bash 4+ also fine
- `curl` - usually pre-installed on macOS/Linux
- `jq` - JSON processor ([install instructions](https://jqlang.github.io/jq/download/))
- Python 3.10+ (only required for the MCP server)

### Installing jq

**macOS** (Homebrew):
```bash
brew install jq
```

**Ubuntu/Debian**:
```bash
sudo apt-get install jq
```

**Fedora/RHEL**:
```bash
sudo dnf install jq
```

**Windows** (via Chocolatey):
```bash
choco install jq
```

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/daniel-pittman/bitbucket-cli.git
   cd bitbucket-cli
   ```

2. Make the script executable:
   ```bash
   chmod +x bb
   ```

3. Symlink to your PATH. Pick the directory that's on your PATH and that you own:
   ```bash
   # macOS with Homebrew (no sudo needed; resolves to /opt/homebrew on
   # Apple Silicon, /usr/local on Intel Macs):
   ln -s "$(pwd)/bb" "$(brew --prefix)/bin/bb"

   # macOS without Homebrew, or Linux (needs sudo on most setups):
   sudo ln -s "$(pwd)/bb" /usr/local/bin/bb
   ```

   Or add the directory to your PATH (pick the rc file your shell uses;
   macOS defaults to zsh since Catalina):
   ```bash
   # bash:
   echo 'export PATH="$PATH:/path/to/bitbucket-cli"' >> ~/.bashrc
   # zsh (default on macOS):
   echo 'export PATH="$PATH:/path/to/bitbucket-cli"' >> ~/.zshrc
   ```

## Configuration

Create a config file at `~/.config/bb/config`:

```bash
mkdir -p ~/.config/bb
cat > ~/.config/bb/config <<EOF
BB_USER=your-email@example.com
BB_TOKEN=your-api-token
BB_WORKSPACE=your-workspace   # optional — see "How the workspace is resolved"
EOF
```

`BB_USER` and `BB_TOKEN` are required. `BB_WORKSPACE` is **optional**: when
you run `bb` inside a Bitbucket git checkout, the workspace is auto-detected
from the `origin` remote, so you only need `BB_WORKSPACE` as a fallback for
commands run outside a repo (e.g. `bb repos` from an arbitrary directory).

### How the workspace is resolved

For any command that needs a workspace, `bb` resolves it in this order
(highest priority first):

1. **`-w/--workspace <name>` flag** — explicit, per-invocation override.
2. **`workspace/slug` argument** — e.g. `bb pipelines acme/widget` targets the
   `acme` workspace regardless of git context or config.
3. **git `origin` auto-detect** — when you're inside a Bitbucket checkout, the
   workspace comes from the remote URL. This is why `cd`-ing into any repo and
   running `bb prs` / `bb pipelines` Just Works across multiple workspaces.
4. **`BB_WORKSPACE`** — your configured default, used when none of the above
   apply (notably for repo-less commands like `bb repos` run outside a checkout).
5. If none resolve a workspace, the command fails with a message naming all
   three ways to supply one.

This mirrors the MCP server's `_resolve_repo` behavior, so the bash CLI and the
Python tools agree on which workspace a given invocation targets.

### Getting an API Token

1. Go to [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click "Create API token"
3. Copy the token and set it as `BB_TOKEN`
4. Set `BB_USER` to your Bitbucket account email address

### Required Bitbucket Permissions

Your Bitbucket account needs these workspace permissions:

| Feature | Required Permission |
|---------|---------------------|
| View pipelines, PRs, repos | **Read** access to repositories (`read:repository:bitbucket`) |
| Trigger/stop pipelines | **Read + Write** access to Pipelines (`read:pipeline:bitbucket`, `write:pipeline:bitbucket`) |
| Create/approve/merge PRs | **Read + Write** access to Pull Requests (`read:pullrequest:bitbucket`, `write:pullrequest:bitbucket`) |
| List workspaces (`bb workspaces`) | `read:workspace:bitbucket` (new in v1.2.0 — Atlassian API token scope, opt-in when creating the token) |

Note: Atlassian API tokens inherit your account's workspace permissions. If you can perform an action in the Bitbucket UI, the CLI can do it too — provided the token carries the scope. The cross-workspace listing endpoints (`/2.0/workspaces`, `/2.0/repositories?role=member`) were removed under Atlassian's [CHANGE-2770](https://developer.atlassian.com/cloud/bitbucket/changelog/) on 2026-04-14; `bb workspaces` uses the replacement `/2.0/user/workspaces` endpoint (CHANGE-3022) which requires the `read:workspace:bitbucket` scope. Rotating the token to add it leaves existing tokens unchanged.

### Environment Variables

You can also set configuration via environment variables:

```bash
export BB_USER="your-email@example.com"
export BB_TOKEN="your-api-token"
export BB_WORKSPACE="your-workspace"
```

## Usage

```
bb <command> [options]
```

### Pipelines

```bash
bb pipelines [repo] [count]           # List recent pipelines (default: 10)
bb pipeline [repo] <number>           # Show pipeline details and steps
bb watch [repo] [number] [interval]   # Poll pipeline until done (default: 15s)
bb logs [repo] <number> [step]        # Show step logs
bb trigger [repo] [branch] [pattern]  # Trigger a pipeline run
bb stop [repo] <number>               # Stop a running pipeline
bb approve [repo] <number>            # Open pipeline in browser (manual steps require UI)
```

### Pull Requests

```bash
bb prs [repo] [state]                 # List PRs (default: OPEN)
bb pr [repo] <id>                     # View PR details
bb pr-create [repo] <title> [dest]    # Create PR from current branch
bb pr-approve [repo] <id>             # Approve a PR
bb pr-merge [repo] <id> [strategy]    # Merge a PR (merge_commit|squash|fast_forward)
bb pr-decline [repo] <id>             # Decline a PR
bb pr-diff [repo] <id>                # Show PR diff
bb pr-comments [repo] <id>            # Show PR comments
```

### Branches & Repositories

```bash
bb branches [repo]                    # List branches
bb repos                              # List workspace repos
bb repo [repo]                        # Show repo details
bb repo-create <name> [opts]          # Create a repo (default PRIVATE)
bb downloads [repo]                   # List repo downloads
bb vars [repo]                        # List pipeline variables
bb vars set [repo] <KEY> [opts]       # Create or update a pipeline variable
```

`bb repo-create` defaults to a **private** repo so a forgotten flag never publishes one. Flags: `--public` / `--private`, `--project KEY` (required on workspaces that use projects), `--description TEXT`.

```bash
bb repo-create widget-service                          # private, default project
bb repo-create widget-service --project WID            # private, in project WID
bb repo-create docs-site --public --description "Docs" # public
```

`bb vars set` creates the variable if its key is new, or updates the existing one. Provide the value via **exactly one** of `--value`, `--value-file`, or `--value-env`. For secrets prefer `--value-file` or `--value-env` so the secret never appears in `argv` / the process list / shell history, and pass `--secured` so Bitbucket masks it. The command never echoes a value back.

```bash
bb vars set widget-service AWS_REGION --value us-east-1            # non-secret
bb vars set widget-service S3_BUCKET_NAME --value my-bucket
bb vars set widget-service AWS_SECRET --secured --value-file ./secret.txt
bb vars set widget-service AWS_KEY --secured --value-env AWS_KEY   # read from env
```

### Utilities

```bash
bb open [repo] [section]              # Open in browser (pr|pipelines|branches|settings|commits)
bb help                               # Show help
```

### Global Flags

```bash
-w, --workspace <name>                # Override workspace for this command
```

### Auto-Detection

When inside a git repository with a Bitbucket remote, the `[repo]` argument is optional — **both the repo slug AND the workspace** are auto-detected from the `origin` remote URL. This is what lets you `cd` between repos in different workspaces and have commands target the right one without a `-w` flag. To override, pass `-w <workspace>`, a `workspace/slug` argument, or set `BB_WORKSPACE` as a default (see [How the workspace is resolved](#how-the-workspace-is-resolved)).

## Examples

```bash
# Watch the latest pipeline on current repo
bb watch

# List open PRs
bb prs

# Create a PR from current branch to main
bb pr-create "Add new feature"

# Trigger a custom pipeline with variables
bb trigger my-repo main manual-deploy-prod LAMBDA_NAMES=mci

# View pipeline logs for step 1
bb logs my-repo 42 1

# Open repo settings in browser
bb open my-repo settings
```

## MCP server (for Claude Code / AI agents)

A Python [Model Context Protocol](https://modelcontextprotocol.io/) server (`mcp_server.py`) ships as a peer to the `bb` bash script. Both implement the **same Bitbucket Cloud REST contract** independently — the MCP server does not shell out to `bb`; it speaks HTTP directly via Python stdlib (no `requests` etc.). Any [Claude Code](https://docs.claude.com/en/docs/claude-code) session — or any other MCP-aware client — can drive Bitbucket Cloud as native tools without invoking the CLI.

### What it exposes

33 tools covering pipelines, pull requests, workspaces, repos, branches, commits, pipeline variables, and git-context helpers:

| Category | Tools |
|---|---|
| Pipelines (read) | `pipelines_list`, `pipeline_show`, `pipeline_steps`, `pipeline_logs` |
| Pipelines (write) | `pipeline_trigger`, `pipeline_stop` |
| Pull requests (read) | `prs_list`, `pr_show`, `pr_activity`, `pr_diff`, `pr_comments_list` |
| Pull requests (write) | `pr_create`, `pr_approve`, `pr_unapprove`, `pr_merge`, `pr_decline`, `pr_comment_add` |
| Workspaces | `workspaces_list` (needs `read:workspace:bitbucket` scope — see [Required Bitbucket Permissions](#required-bitbucket-permissions)) |
| Repos / metadata | `repos_list`, `repo_show`, `repo_create`, `branches_list`, `branch_show`, `commits_list`, `vars_list`, `vars_set`, `downloads_list` |
| Git context | `git_current_branch`, `git_status`, `git_remote_repo`, `git_recent_commits`, `git_uncommitted_changes` |
| Meta | `whoami` (see note below) |

Note on `whoami`: resolves config + git context + a workspace-reachability probe (single low-cost `GET /repositories/{workspace}?pagelen=1`, 10 s timeout). Never echoes `BB_TOKEN`. The probe requires `repository:read` scope — a workspace-scoped token granting only `pipelines:read` or `pullrequest:read` will report `auth.ok=False` even though pipeline/PR tools still work, so treat the probe as a scope hint rather than a global credential verdict.

Every tool that takes a repo argument supports auto-detection (omit `repo` to resolve from the current git checkout's `origin` remote — or from `BB_DEFAULT_REPO_PATH` if set; see [Environment overrides](#environment-overrides) below) and workspace override (`workspace/repo` shape).

### MCP server requirements

- Python 3.10+ available on PATH (the bash CLI doesn't need Python — only the MCP server does).
- The same `~/.config/bb/config` (or `BB_USER` / `BB_TOKEN` / `BB_WORKSPACE` env vars) as the CLI — see [Configuration](#configuration) above.

### MCP server install

```bash
# 1. Make sure bb itself is installed and configured (see Configuration above).

# 2. Register the MCP server with Claude Code (user scope = all sessions on this machine):
claude mcp add --scope user bitbucket \
    -- python3 /absolute/path/to/bitbucket-cli/mcp_server.py  # ← replace with your clone path

# `python3` is intentionally bare — `claude mcp add` inherits PATH, so a
# Homebrew or pyenv Python 3.10+ resolves naturally. Do NOT hardcode
# /usr/bin/python3 on macOS: Apple's bundled Python at that path is 3.9,
# which is below the MCP server's 3.10 minimum. The `--` separator before
# `python3` keeps the command robust if you later add `--env` flags
# (see "Multiple workspaces" below).

# 3. On first invocation, the server self-bootstraps a durable venv at
#    $XDG_DATA_HOME/bitbucket-cli/venv (default: ~/.local/share/bitbucket-cli/venv)
#    and installs the `mcp` package into it. Subsequent launches reuse the venv.
#
#    To force a clean rebuild:
#      rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/bitbucket-cli/venv"
#    and relaunch the MCP server.

# 4. Verify the connection (handshake only — does NOT validate credentials):
claude mcp list
# Should show: bitbucket: ... - ✓ Connected
#
# First invocation may briefly show "✗ Failed to connect" while the venv
# bootstraps (pip-installs `mcp`, 5-30 s depending on network). Retry once.

# 5. Verify credentials in a Claude Code session by asking it to run the
#    `whoami` tool — `Connected` above only confirms the stdio handshake;
#    `whoami` confirms BB_USER/BB_TOKEN/BB_WORKSPACE actually resolve AND
#    the token reaches your configured workspace.
```

**Multiple workspaces:** to register more than one server (e.g. `bitbucket-work` and `bitbucket-personal` pointing at different workspaces), use `--env` flags per registration so each server entry carries its own credentials:

```bash
claude mcp add --scope user bitbucket-work \
    --env BB_USER=you@work.com \
    --env BB_TOKEN=... \
    --env BB_WORKSPACE=acme \
    -- python3 /absolute/path/to/bitbucket-cli/mcp_server.py  # ← replace with your clone path
```

### Other MCP clients

`mcp_server.py` is a stdio MCP server, so any client that speaks MCP-over-stdio can use it. The command is `python3 /absolute/path/to/bitbucket-cli/mcp_server.py` — same Python-version constraint as above. Credentials come from `~/.config/bb/config` or environment variables; clients that strip `HOME` from the subprocess environment need to pass `BB_USER` / `BB_TOKEN` / `BB_WORKSPACE` explicitly instead.

### Environment overrides

| Variable | Purpose |
|---|---|
| `BB_API_BASE` | Override the Bitbucket REST base URL (default `https://api.bitbucket.org/2.0`). Useful for a test / proxied / staging mirror. |
| `BB_DEFAULT_REPO_PATH` | Default working directory for repo auto-detection (when a Bitbucket tool is called with `repo=""`) AND for the `git_*` tools' default `path=""` resolution. Defaults to the MCP server's launch cwd. |
| `XDG_DATA_HOME` | Standard XDG override for the data root. The venv lives at `$XDG_DATA_HOME/bitbucket-cli/venv` (default `~/.local/share/bitbucket-cli/venv`). |

### Optional: install the bundled `bitbucket` agent for delegated use

The MCP server exposes the *tools*; the bundled **agent** (`agents/bitbucket.md` in this repo) is the *behavioral layer* that makes a Claude Code session use those tools intelligently — propose-first protocol for destructive ops (`pr_merge`, `pr_decline`, `pipeline_stop`, `pr_unapprove`), resolve-git-context-first before any Bitbucket call, show-diffs-before-merge discipline, bash/Python parity rule for delegated CLI maintenance, and the per-workspace conventions block for tracking each workspace's defaults.

The bundled `agents/bitbucket.md` is a **deliberately-generic template** — it ships with placeholder examples (`acme/widget-service`, fictional reviewers, generic custom-pipeline patterns like `deploy-prod`) and an explicitly-blank "Per-workspace conventions" section at the bottom. After copying it to `~/.claude/agents/bitbucket.md`, personalize your local copy with your default workspace, required reviewers, custom pipeline patterns, branch naming conventions, and any other non-generic context. **Anything you contribute back to this repo via PR should be re-genericized first** — real workspace slugs, real ticket titles, real reviewer handles, and project-specific custom-pipeline patterns belong only in your personal `~/.claude/agents/` copy, never in the upstream-tracked version.

The agent is a single Markdown file with frontmatter. To install:

```bash
# 1. Copy the agent definition into user-scope agents.
mkdir -p ~/.claude/agents
cp agents/bitbucket.md ~/.claude/agents/bitbucket.md

# 2. Populate the "Per-workspace conventions" section using bb itself —
#    don't type defaults from memory, let the CLI discover them:
#      bb workspaces                # which workspaces you belong to
#      bb -w <ws> repos             # what's in each
#      bb repo <ws>/<repo>          # → Main branch (verify PR base; GitFlow repos target develop)
#      bb pipelines <ws>/<repo>     # → TRIGGER column = custom pipelines
#      bb branches <ws>/<repo>      # → branch-naming convention
#      bb vars <ws>/<repo>          # → SECURED=true = sensitive vars to mask
#    Add one block per workspace (the template at the bottom of the agent
#    file shows the shape). The bundled agent can run this survey for you.

# 3. Newly-started Claude Code sessions pick up the agent automatically.
#    Existing sessions need a restart. In any new session you can then
#    delegate to it:
#
#    "Use the bitbucket agent to merge PR 42"
#    "Have the bitbucket agent watch pipeline 142"
#    "Ask the bitbucket agent to trigger a deploy-prod run on main"
```

The agent description tells Claude Code's orchestrator when to delegate to it automatically (e.g. when the user mentions Bitbucket pipeline / PR / repo operations). You don't have to invoke it by name.

What the agent enforces on top of the raw tools:

The MCP tools already do per-call auto-detection on their own (source-branch auto-detect on `pr_create`, repo auto-detect on every Bitbucket tool given `repo=""`) — the agent doesn't re-implement those. What the agent adds:

| Behavior | Raw MCP tools | With bundled `bitbucket` subagent |
|---|---|---|
| **Destructive ops** (`pr_merge`, `pr_decline`, `pipeline_stop`, `pr_unapprove`) | Fired immediately when invoked | Propose-first: show diff / activity / current state, confirm with user, then act |
| **Pipeline failure investigation** | Caller must navigate `pipeline_show` → `pipeline_steps` → `pipeline_logs` manually | Triages in that order, surfaces the relevant log tail (last ~50 lines around the failure) instead of dumping the whole stream |
| **Avoiding redundant probes** | Caller may re-fetch `git_current_branch` / `git_remote_repo` per call even when the tool would auto-detect | Lets tool-level auto-detect carry the call (passes `repo=""` and omits `source_branch=` instead of pre-fetching git context just to echo it back) |
| **`bb`-CLI maintenance** (delegated) | Re-discovers the parity rule, naming conventions, redaction patterns per session | Owns the design → implement → test → docs → PR cycle with the rules already baked in |
| **Per-workspace conventions** | Re-discovered each session | Reads (or bb-discovers and proposes) the agent file's "Per-workspace conventions" block for the active workspace before any write op |

### Security

- `BB_TOKEN` is never echoed (`whoami`, error envelopes, log lines).
- URL credentials (`https://user:token@host/...`) and signed-URL query parameters (AWS / Azure / GCP / bearer / access_token / api_key) are stripped from every error message.
- Cross-host `Authorization` headers are stripped on redirect so the Bitbucket Basic header never reaches S3 when fetching pipeline logs.
- Pipeline variable values are masked as `KEY=***` when echoed back.

The agent file is genuinely portable — strip the example "Per-workspace conventions" section and you have a clean template that works for any Bitbucket Cloud workspace.

## License

MIT License - see [LICENSE](LICENSE) for details.
