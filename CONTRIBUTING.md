# Contributing to `bb` (Bitbucket CLI)

Thanks for your interest. This project is small and bash-script-driven; the contribution workflow is correspondingly lightweight.

## Filing issues

Open a regular [GitHub issue](https://github.com/daniel-pittman/bitbucket-cli/issues) for anything except security reports — bug reports, feature requests, doc fixes, questions about behaviour.

**Security issues** go through [`SECURITY.md`](SECURITY.md), not the public tracker. See its "Reporting a vulnerability" section.

## Pull request workflow

1. Fork the repo and create a feature branch off `develop` (the default branch).
2. Make your change. The required `syntax` CI check runs:
   - `bash -n bb` (bash parser, catches syntax errors before runtime)
   - presence checks for `bash`, `jq`, `curl` on the runner
3. Open the PR against `develop`. The `main` branch only receives release PRs from `develop` (it's the stable line that tags point at).
4. Address review feedback. Once approved and CI is green, a maintainer will merge.

The first time an outside contributor opens a PR, GitHub holds Actions execution pending maintainer approval — this is the "Require approval for all outside collaborators" gate documented in [`SECURITY.md` §1](SECURITY.md). The PR is fine; it just may sit briefly before workflows start.

## CI and review automation

- **`syntax` (required)** — bash syntax check, runtime-tooling presence check, and the Python test suite across Python 3.10 / 3.11 / 3.12. Must pass before merge into `develop` or `main`. No secrets needed; runs on every push and PR including from forks.
- **Claude code review (advisory)** — automated PR review using a subscription-bound OAuth token. Output appears as a PR comment; not a merge gate.
- **Claude security review (advisory)** — runs only on PRs targeting `main` or `develop`. Uses a metered Anthropic API key; also advisory.

The interactive `@claude` bot is available for maintainer-triggered triage. **Only comments authored by `OWNER`, `MEMBER`, or `COLLABORATOR` accounts trigger it**, by design — outside contributors who type `@claude` won't get a response, to bound subscription-quota burn. See [`SECURITY.md` §6](SECURITY.md) for the rationale.

## Code style

- Functions are named `cmd_*` for user-facing commands. New subcommands should mirror the closest existing analogue.
- Helper functions for HTTP transport: `bb_get`, `bb_post`, `bb_put`, `bb_delete`. New API calls should route through these so auth handling stays in one place.
- `detect_repo()` is the auto-detect entry point for resolving a repo from the local git remote. Reuse it; don't duplicate the parsing.
- Updates to commands should be reflected in `README.md`, `CLAUDE.md`, and the inline `cmd_help()` block together — they all describe the same surface from different angles.

## Bash and Python are parallel implementations

The `bb` bash script and the Python modules (`bb_api.py`, `bb_ops.py`, `mcp_server.py`) are **parallel implementations of the same Bitbucket REST contract**, not layered on top of each other:

```
bb (bash)   <-->   Bitbucket REST API   <-->   bb_api / bb_ops (Python)
                       (single source
                        of truth)
```

The MCP server talks to Bitbucket directly via Python; it does not shell out to `bb` and parse its output.

**The parity rule:** when a test in `tests/test_bb_*.py` surfaces a defect in URL construction, body shape, parameter naming, or auth handling, the fix lands in the Python module *and* in `bb` if `bb` has the parallel logic. Tests verify the **correct** API contract — they do not pin existing buggy behaviour on either side. If you find yourself writing a test that only passes against the current implementation despite knowing the contract is wrong, that's a signal to fix the code, not pin the bug.

Practical workflow when adding or changing an endpoint call:

1. Write the test against what the Bitbucket API documents should happen.
2. Implement it correctly in the Python module.
3. Inspect `bb` — does it call the same endpoint with the same shape? If yes, mirror the fix.
4. Update both code paths in the same PR.

## Testing

Run the Python suite locally:

```bash
python -m pip install pytest
python -m pytest tests/ -v
```

The suite mocks HTTP at `urllib.request` and does not need a live Bitbucket workspace. CI runs the same suite across Python 3.10 / 3.11 / 3.12 in addition to the `bash -n bb` syntax check.

Manual coverage for the bash side is still on you when touching `bb` — at minimum:

```bash
bash -n bb              # syntax check, same as CI
bb help                 # smoke-test the dispatcher
bb repos                # round-trip a real API call against your own workspace
```

## No personal data in public code

Examples, fixtures, docs, and comments must use **fictional names** and **generic institutional abbreviations** — not real colleagues, organizations, repositories, or workspace slugs from the maintainer's day-to-day use. Real names land in public git history permanently. Default substitutes:

- Personal names: Alice Garcia, Bob Jones, Carol Lee.
- Institutional / workspace abbreviations: `acme`, `widget-co`, `example-workspace`.
- Emails: `user@example.com` (RFC-reserved domain; cannot accidentally collide).

The discipline is at the moment of writing — easier than scrubbing later.

