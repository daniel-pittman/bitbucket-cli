# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Overview

`bb` is a bash CLI wrapper around the Bitbucket Cloud REST API. It provides commands for managing pipelines, pull requests, branches, and repositories from the terminal.

## Project Structure

```
bitbucket-cli/
├── bb                 # Main CLI script (bash)
├── README.md          # User documentation
├── LICENSE            # MIT license
├── CLAUDE.md          # This file
├── .env.example       # Example environment config
└── .gitignore         # Git ignore rules
```

## Key Technical Details

- **Language**: Pure bash (no external dependencies beyond curl and jq)
- **API**: Bitbucket Cloud REST API v2.0 (`https://api.bitbucket.org/2.0`)
- **Auth**: HTTP Basic authentication using Atlassian API tokens
  - `BB_USER`: Bitbucket account email address
  - `BB_TOKEN`: API token from id.atlassian.com
  - `BB_WORKSPACE`: Bitbucket workspace slug

## Configuration

The script loads config from two locations (in order):
1. `~/.config/bb/config` - User config file
2. `.env` in script directory - Local override (gitignored)

## Code Conventions

- Functions are named `cmd_*` for user-facing commands
- Helper functions: `bb_get`, `bb_post`, `bb_put`, `bb_delete` for API calls
- `detect_repo()` auto-detects repo from git remote if not provided
- `format_state()` normalizes pipeline/PR states to 4-char display codes

## Adding New Commands

1. Create a `cmd_yourcommand()` function
2. Add it to the case statement at the bottom of the script
3. Add help text in `cmd_help()`
4. Update README.md with usage examples

## Testing

Manual testing against a Bitbucket workspace:

```bash
# Verify auth works
bb repos

# Test pipeline commands
bb pipelines your-repo
bb pipeline your-repo 1

# Test PR commands
bb prs your-repo
```

## Known Limitations

- Manual pipeline step approval requires the Bitbucket UI (API doesn't support it)
- Large log outputs may be truncated by the API
- Rate limiting is not handled (Bitbucket has generous limits)
