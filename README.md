# bb - Bitbucket CLI

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/daniel-pittman/bitbucket-cli/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/daniel-pittman/bitbucket-cli/actions/workflows/ci.yml)
[![Bash](https://img.shields.io/badge/bash-4.0%2B-1f425f.svg)](https://www.gnu.org/software/bash/)
[![GitHub release](https://img.shields.io/github/v/release/daniel-pittman/bitbucket-cli)](https://github.com/daniel-pittman/bitbucket-cli/releases)

A lightweight command-line interface for Bitbucket Cloud. Wraps the Bitbucket REST API for common operations like managing pipelines, pull requests, and repositories.

No dependencies beyond `curl` and `jq`. Works on macOS, Linux, and WSL.

## Features

- **Pipelines**: List, view, watch, trigger, and stop pipeline builds
- **Pull Requests**: Create, view, approve, merge, and manage PRs
- **Repositories**: List repos, view details, browse branches
- **Browser Integration**: Quick-open any resource in your browser

## Requirements

- `bash` (4.0+)
- `curl` - usually pre-installed on macOS/Linux
- `jq` - JSON processor ([install instructions](https://jqlang.github.io/jq/download/))

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

## License

MIT License - see [LICENSE](LICENSE) for details.
