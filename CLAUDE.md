# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Overview

`bb` is a Bitbucket Cloud client with two parallel implementations of the same REST contract:

- **Bash CLI** (`bb`) — human-facing, depends only on `curl` + `jq`.
- **Python MCP server** (`mcp_server.py` + `bb_api.py` + `bb_ops.py` + `git_ops.py`) — exposes the same Bitbucket surface plus git-context helpers as MCP tools that Claude Code (or any MCP-aware client) can call directly.

Both implementations target Bitbucket Cloud REST API v2.0. Neither wraps the other — they speak HTTP directly.

## Project Structure

```
bitbucket-cli/
├── bb                  # Bash CLI (cmd_* functions, ~1.4k lines)
├── bb_api.py           # urllib-based HTTP client, pagination, redacting
├── bb_ops.py           # Bitbucket operations: pipelines / PRs / repos / branches / vars / commits
├── git_ops.py          # subprocess wrappers: branch / status / remote / commits / diffs
├── mcp_server.py       # FastMCP tool registry + self-bootstrap venv
├── agents/bitbucket.md # Generic agent definition for the MCP server
├── tests/              # pytest suite (~360 tests)
├── pyproject.toml      # Python packaging + pytest config
├── docs/img/           # social-preview.png and other assets
├── README.md           # User-facing docs (install, usage, MCP setup)
├── CONTRIBUTING.md     # Parity rule, GitFlow, branch protection
├── SECURITY.md         # Disclosure policy + maintainer checklist
└── .github/            # CI, Claude code-review / security-review workflows, CODEOWNERS
```

## Key Technical Details

- **Languages**: bash (3.2+ — macOS system bash is supported) and Python (3.10+)
- **API**: Bitbucket Cloud REST API v2.0 (`https://api.bitbucket.org/2.0`)
- **Auth**: HTTP Basic using Atlassian API tokens
  - `BB_USER`: Bitbucket account email
  - `BB_TOKEN`: API token from id.atlassian.com (never echoed)
  - `BB_WORKSPACE`: workspace slug
- **MCP runtime**: stdlib-only at runtime; the `mcp` package is the only third-party dep, installed into a self-bootstrapped venv at `$XDG_DATA_HOME/bitbucket-cli/venv` (default `~/.local/share/bitbucket-cli/venv`) on first invocation.

## Configuration

Loaded in this order (later overrides earlier):
1. `~/.config/bb/config` — user config
2. `.env` in script directory — local override (gitignored)
3. Environment variables (highest priority)

## Code Conventions

### Bash (`bb`)
- User-facing commands: `cmd_<name>` functions.
- HTTP helpers: `bb_get` / `bb_post` / `bb_put` / `bb_delete`.
- `resolve_repo` sets `repo` + `BB_WORKSPACE` in the CALLER's scope (called as `resolve_repo "$1"`, NOT `repo=$(...)`, so it escapes the subshell and can set the workspace). Precedence: `-w` flag > `workspace/slug` arg > git origin auto-detect > `BB_WORKSPACE` default > error. `resolve_workspace` is the repo-less companion for workspace-level commands. Both mirror the Python `_resolve_repo` contract.
- `BB_WORKSPACE` is OPTIONAL — only `BB_USER` + `BB_TOKEN` are required at load; a missing workspace fails at the point of use, not at startup.
- Boundary validation via helpers like `_require_build_number` (rejects non-numeric) and `_require_pr_state` (allowlists OPEN/MERGED/DECLINED/SUPERSEDED — also closes a query-param injection surface).
- Variables are passed to `jq -Rs` with NUL delimiters to prevent injection.
- Error rc capture: `local rc=0; out=$(cmd) || rc=$?` — NOT `if ! cmd; then rc=$?` (the `!` negation makes `$?` always 0; verified on bash 3.2 + 5.x).
- bash 3.2+ floor (macOS system bash supported) — no `${var,,}` / `${var^^}` / `mapfile` / `declare -A`; a `bash:3.2` CI job (`bash32-floor`) parses `bb` to catch regressions.

### Python (`bb_api.py`, `bb_ops.py`, `git_ops.py`, `mcp_server.py`)
- `BBClient` injected as first arg into every `bb_ops` function.
- `bb_ops.<verb>_<noun>` naming (e.g. `pipeline_trigger`, `pr_create`).
- `_is_positive_int` guard rejects bool (the bool-is-int trap).
- MCP tools resolve repo via `_resolve_repo` (rejects malformed/`.`/`..`/whitespace BEFORE any network call).
- Error envelopes route ALL string fields (`message`, `body`, `stderr`, `url`) through `_safe_text` / `_redact_url` — single chokepoint, not per-field.
- `_error_dict_with(e, ...)` threads request identifiers (pr_id, number, step_index) into the error dict for correlation.

### Parallel-implementation parity rule
The bash and Python sides implement the **same Bitbucket REST contract** independently. When a defect surfaces in either side (URL construction, body shape, parameter naming), the fix lands in BOTH paths. Tests verify the correct contract — never pin existing buggy behavior. See CONTRIBUTING.md.

## Adding New Commands

For end-user CLI features:
1. Add `cmd_yourcommand()` in `bb`.
2. Wire into the case statement at the bottom of the script.
3. Add help text in `cmd_help()`.
4. Update README.md usage examples.

For MCP-tool features:
1. Add `bb_ops.<verb>_<noun>()` Python function.
2. Add pytest coverage in `tests/` (assert URL + method + body shape — not just status).
3. Add a thin `@mcp.tool()` wrapper in `mcp_server.py` that calls `_resolve_repo`, invokes the ops function, returns `{"ok": True, ...}` on success and `_error_dict_with(e, ...)` on failure.
4. Update the tool-surface table in `agents/bitbucket.md`.

If the new feature applies to both surfaces (the dominant case), do both. Parity is the default.

## Testing

```bash
# Python tests (run from repo root)
pytest

# Specific test file
pytest tests/test_mcp_server.py -v

# Bash smoke tests (manual, against your workspace)
bb whoami
bb repos
bb pipelines your-repo
```

CI runs `pytest` on every PR. Bash side is smoke-tested manually before release.

## Security Posture

- BB_TOKEN never echoed (whoami, error dicts, log lines).
- URL credential leaks (`https://user:token@host`) stripped in all redactors.
- Signed-URL query parameters (AWS X-Amz-Signature, Azure SAS, GCP signing, bearer / access_token / api_key) stripped from error URLs.
- Cross-host Authorization stripping on redirect (so the Bitbucket Basic header never reaches S3).
- Pipeline variable values masked as `KEY=***` when echoed back to the user.
- subprocess calls use `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=""`, `stdin=DEVNULL`, timeout — no interactive prompts can hang the MCP server.
- See `SECURITY.md` for the full posture and disclosure policy.

## Known Limitations

- Manual pipeline step approval requires the Bitbucket UI (REST API doesn't support it).
- Diffs from `git_uncommitted_changes` are capped at 1 MiB (with truncation marker); untracked file list capped at 10 000 entries.
- Rate limiting is not handled explicitly (Bitbucket Cloud has generous limits; if you hit one, the MCP error envelope surfaces the 429 cleanly).
