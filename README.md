# bb - Bitbucket CLI

<p align="center">
  <img src="docs/img/social-preview.png" alt="bb — Bitbucket Cloud CLI" width="720" />
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/daniel-pittman/bitbucket-cli/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/daniel-pittman/bitbucket-cli/actions/workflows/ci.yml)
[![Bash](https://img.shields.io/badge/bash-4.0%2B-1f425f.svg)](https://www.gnu.org/software/bash/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![GitHub release](https://img.shields.io/github/v/release/daniel-pittman/bitbucket-cli)](https://github.com/daniel-pittman/bitbucket-cli/releases)

A lightweight command-line interface for Bitbucket Cloud. Wraps the Bitbucket REST API for common operations like managing pipelines, pull requests, and repositories. Ships with a Python [MCP server](#mcp-server-for-claude-code--ai-agents) so any [Claude Code](https://docs.claude.com/en/docs/claude-code) session (or any other MCP-aware client) can drive Bitbucket Cloud as native tools.

The bash CLI has no dependencies beyond `curl` and `jq`. The MCP server adds Python 3.10+. Works on macOS, Linux, and WSL.

## Features

- **Pipelines**: List, view, watch, trigger, and stop pipeline builds
- **Pull Requests**: Create, view, approve, unapprove, merge, decline, diff, comment
- **Repositories**: List repos, view details, list/show branches, list recent commits
- **Browser Integration**: Quick-open any resource in your browser
- **MCP server**: 30 tools covering the full surface, plus git-context wrappers (current branch, status, recent commits, uncommitted changes) for agent workflows

## Requirements

- `bash` (4.0+)
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

3. Symlink to your PATH:
   ```bash
   ln -s "$(pwd)/bb" /usr/local/bin/bb
   ```

   Or add the directory to your PATH:
   ```bash
   echo 'export PATH="$PATH:/path/to/bitbucket-cli"' >> ~/.bashrc
   ```

## Configuration

Create a config file at `~/.config/bb/config`:

```bash
mkdir -p ~/.config/bb
cat > ~/.config/bb/config <<EOF
BB_USER=your-email@example.com
BB_TOKEN=your-api-token
BB_WORKSPACE=your-workspace
EOF
```

### Getting an API Token

1. Go to [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click "Create API token"
3. Copy the token and set it as `BB_TOKEN`
4. Set `BB_USER` to your Bitbucket account email address

### Required Bitbucket Permissions

Your Bitbucket account needs these workspace permissions:

| Feature | Required Permission |
|---------|---------------------|
| View pipelines, PRs, repos | **Read** access to repositories |
| Trigger/stop pipelines | **Read + Write** access to Pipelines |
| Create/approve/merge PRs | **Read + Write** access to Pull Requests |

Note: Atlassian API tokens inherit your account's workspace permissions. If you can perform an action in the Bitbucket UI, the CLI can do it too.

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
bb downloads [repo]                   # List repo downloads
bb vars [repo]                        # List pipeline variables
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

When inside a git repository with a Bitbucket remote, the `[repo]` argument is optional - it will be auto-detected from the git remote URL.

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

`bb` ships with a [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes 30 tools — pipelines, pull requests, repos, branches, commits, repo variables, and git-context helpers — so [Claude Code](https://docs.claude.com/en/docs/claude-code) (or any other MCP-aware client) can drive Bitbucket Cloud as native tools.

The MCP server is a **parallel implementation** of the same Bitbucket REST contract as the bash CLI — it does not shell out to `bb`. It speaks HTTP directly via Python stdlib (no `requests` etc.).

### Requirements

- Python 3.10+ (only required for the MCP server; the bash CLI does not need Python)
- The same `BB_USER` / `BB_TOKEN` / `BB_WORKSPACE` credentials as the CLI

### Install

The MCP server self-bootstraps a virtual environment on first run, so installation is just two steps: clone the repo (see [Installation](#installation) step 1), then point your MCP client at `mcp_server.py`. The first invocation creates `~/.local/share/bitbucket-cli/venv` (relocate by setting `XDG_DATA_HOME`; the venv then lives at `$XDG_DATA_HOME/bitbucket-cli/venv`) and installs the `mcp` package. Subsequent invocations reuse the venv — startup is fast.

### Claude Code (`.mcp.json` or `claude mcp add`)

Add the server to your Claude Code MCP config. The simplest form:

```json
{
  "mcpServers": {
    "bitbucket": {
      "command": "python3",
      "args": ["/absolute/path/to/bitbucket-cli/mcp_server.py"],
      "env": {
        "BB_USER": "your-email@example.com",
        "BB_TOKEN": "your-api-token",
        "BB_WORKSPACE": "your-workspace"
      }
    }
  }
}
```

Or via the CLI:

```bash
claude mcp add bitbucket \
  --env BB_USER=your-email@example.com \
  --env BB_TOKEN=your-api-token \
  --env BB_WORKSPACE=your-workspace \
  -- python3 /absolute/path/to/bitbucket-cli/mcp_server.py
```

### Other MCP clients

`mcp_server.py` is a stdio MCP server, so any client that speaks MCP-over-stdio can use it. The `command` is `python3 /absolute/path/to/bitbucket-cli/mcp_server.py`; environment variables are the same three credentials above.

### Tool surface (30 tools)

| Area | Tools |
|---|---|
| Pipelines (read) | `pipelines_list`, `pipeline_show`, `pipeline_steps`, `pipeline_logs` |
| Pipelines (write) | `pipeline_trigger`, `pipeline_stop` |
| Pull requests (read) | `prs_list`, `pr_show`, `pr_activity`, `pr_diff`, `pr_comments_list` |
| Pull requests (write) | `pr_create`, `pr_approve`, `pr_unapprove`, `pr_merge`, `pr_decline`, `pr_comment_add` |
| Repos / metadata | `repos_list`, `repo_show`, `branches_list`, `branch_show`, `commits_list`, `vars_list`, `downloads_list` |
| Git context | `git_current_branch`, `git_status`, `git_remote_repo`, `git_recent_commits`, `git_uncommitted_changes` |
| Meta | `whoami` (resolves config + connectivity smoke test; never echoes `BB_TOKEN`) |

Every tool that takes a repo argument supports auto-detection (omit `repo` to resolve from the current git checkout's `origin` remote) and workspace override (`workspace/repo` shape).

### Agent definition

A generic agent definition that documents the tool surface, operating principles (resolve-git-context-first, show-diffs-before-destructive-ops, parity discipline), and worked examples lives at [`agents/bitbucket.md`](agents/bitbucket.md). Copy it into your Claude Code agent directory if you want a dedicated subagent for Bitbucket work.

### Security

- `BB_TOKEN` is never echoed (`whoami`, error envelopes, log lines).
- URL credentials (`https://user:token@host/...`) and signed-URL query parameters (AWS / Azure / GCP / bearer / access_token / api_key) are stripped from every error message.
- Cross-host `Authorization` headers are stripped on redirect so the Bitbucket Basic header never reaches S3 when fetching pipeline logs.
- Pipeline variable values are masked as `KEY=***` when echoed back.

## License

MIT License - see [LICENSE](LICENSE) for details.
