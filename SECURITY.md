# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.** A public
issue discloses the problem before a fix is available.

Instead, use **GitHub Private Vulnerability Reporting**:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Describe the issue, the impact, and steps to reproduce.

The maintainer will acknowledge the report, work on a fix, and coordinate a
disclosure timeline with you. `bb` is a local CLI that talks to the Bitbucket
Cloud REST API on behalf of the user; the realistic threat surface is small
(the script handles an Atlassian API token that the user pastes into
`~/.config/bb/config` or sets in their environment), but all reports are
welcome.

## Supported versions

Security fixes are applied to the latest release on `main` and shipped as
patch releases on the current minor line. Development integration happens on
`develop`; releases are cut by merging `develop` -> `main` and tagging.

---

## Repository setup checklist (for maintainers)

The CI and review workflows in this repository are designed to be safe on a
**public** repository, but several protections cannot be committed as files —
they are GitHub repository settings. After creating or forking the repository,
a maintainer must apply all of the following:

### 1. Restrict fork pull-request workflows

**Settings → Actions → General → Fork pull request workflows from outside
collaborators** → set to **"Require approval for all outside collaborators"**.

This means a maintainer must approve each workflow run requested by an outside
contributor's PR, preventing drive-by Actions execution. Combined with the
Claude review workflows below, this is what bounds drive-by subscription-quota
or API-credit burn from random fork PRs.

### 2. Require SHA-pinned actions

**Settings → Actions → General → "Require actions to be pinned to a
full-length commit SHA"** → enable.

The workflow YAML in this repository already pins every third-party action to
a full commit SHA with a version comment. This repo-level setting backstops
the convention — if a future PR adds a workflow that uses `actions/foo@v1` or
`actions/foo@main`, GitHub refuses to run it. Defense against pin-discipline
regression as the project evolves.

### 3. Branch protection on `main` and `develop`

This repository follows a gitflow-style two-branch model: `develop` is the
default integration branch and `main` carries tagged releases. Both branches
need protection.

Under Settings → Branches → Add branch protection rule, for **both** `main`
and `develop`:

- Require a pull request before merging.
- Require **1 approving review**.
- **Dismiss stale pull-request approvals when new commits are pushed.**
- Require status checks to pass before merging — select the **`syntax`** check
  (the job defined in `.github/workflows/ci.yml`). The context name must match
  the job name exactly; an empty `required_status_checks.contexts` list leaves
  the branch looking gated but does not actually block on CI failure.
- Require **conversation resolution** before merging.
- Require **linear history** (no merge commits — rebase or squash only).
- Do **not** allow force pushes.
- Do **not** allow deletions.
- (Recommended) Require review from Code Owners, so `.github/` changes are
  gated by `CODEOWNERS`.

Solo-maintainer note: GitHub does not allow self-approving your own PR. The
agreed pattern is **admin override** with a commit message stating "self-merge
with rationale". This keeps the audit trail clean and makes the override
visible. Do *not* set `enforce_admins: true` on a solo project — it traps the
maintainer with no path forward.

### 4. Verify the CODEOWNERS owner is a direct collaborator

The owner named in `.github/CODEOWNERS` (`@daniel-pittman`) must be a **direct
collaborator** on the repository — or a member of a team with direct
repository access. Permissions inherited only through an organization role do
**not** satisfy CODEOWNERS enforcement: GitHub silently treats the owner as
invalid and the review requirement does not block. Confirm the owner appears
under **Settings → Collaborators and teams**.

If you fork this repository under a different account, replace
`@daniel-pittman` with your handle before enabling CODEOWNERS enforcement.

### 5. Enable secret scanning and push protection

**Settings → Code security and analysis**:

- Enable **Secret scanning**.
- Enable **Push protection** (blocks commits that contain detected secrets).
- Enable **Dependency graph**.

All free for public repositories. Push protection catches the case where a
maintainer accidentally commits a `BB_TOKEN` or `ANTHROPIC_API_KEY` in a
config file or test fixture.

### 6. Configure the Claude review secrets

The repository ships three optional Claude-driven workflows. Each one is
**inert** until its secret is provisioned, so you can enable them
independently as you decide what's worth running.

#### `CLAUDE_CODE_OAUTH_TOKEN` — used by `claude-code-review.yml` and `claude.yml`

These two workflows draw against a Claude **subscription** (not metered API
billing). Provision the token by running

```
claude /install-github-app
```

from a local Claude Code session in this repository. The command installs the
official Claude Code GitHub App on the repo and writes the OAuth token to
**Settings → Secrets and variables → Actions** as `CLAUDE_CODE_OAUTH_TOKEN`.

When the install flow offers to drop in workflow files, **decline** — the
upstream defaults lack the SHA-pinning, author-association gate, and
branch-scoping that the hardened workflows in this repo already provide.

- `claude-code-review.yml` runs automatically on every pull request. The
  outside-collaborator approval gate (§1 above) is what bounds drive-by
  subscription-quota burn from random fork PRs: a first-time outside
  contributor's first workflow run must be approved by a maintainer before
  any Action executes.
- `claude.yml` (the interactive `@claude` bot) runs only when the commenter /
  issue author has at least **COLLABORATOR** access on the repository. Random
  outside users cannot trigger it even by including `@claude` in their text.

#### `ANTHROPIC_API_KEY` — used by `claude-security-review.yml`

The security-review action does not currently support OAuth, so this workflow
uses a metered API key. Add it under **Settings → Secrets and variables →
Actions → New repository secret** as `ANTHROPIC_API_KEY`. The workflow runs
automatically on every PR whose base branch is `main` or `develop`, and can
also be dispatched manually. As with the code-review workflow, the
outside-collaborator approval gate bounds drive-by API-credit burn from
random fork PRs.

---

## Defense-in-depth summary

The protections in this repository are layered so that no single failure
opens a credential-exfiltration path:

1. **`pull_request` trigger (not `pull_request_target`)** — secrets are never
   exposed to fork-PR code.
2. **Outside-collaborator approval gate** — a maintainer must approve the
   first workflow run from any new outside contributor.
3. **Author-association gate on `claude.yml`** — only OWNER / MEMBER /
   COLLABORATOR comments can trigger the interactive bot, even though anyone
   can include `@claude` in text.
4. **SHA-pinned actions** + repo-level "Require actions to be pinned to a
   full-length commit SHA" setting — no upstream tag can silently swap in a
   malicious version, and no future workflow can regress the convention.
5. **Minimal workflow permissions** — each workflow grants only the scopes it
   actually needs.
6. **CODEOWNERS on `.github/` and core sources** — workflow changes require a
   maintainer review.
7. **Branch protection** — the protected branches (`main` and `develop`)
   cannot accept a push that hasn't been through PR review with the `syntax`
   check green.

Defeating one layer (e.g. social-engineering a maintainer into approving a
fork run) still leaves five others between an attacker and any persistent
compromise.

---

## Operator threat surface for `bb` itself

`bb` is a thin client around the Bitbucket Cloud REST API. The credentials
it handles:

- **`BB_USER`** — your Atlassian account email. Not a secret on its own.
- **`BB_TOKEN`** — Atlassian API token. Treat as a password equivalent. It
  inherits whatever workspace permissions your Bitbucket account has; if your
  account can push, the token can push.
- **`BB_WORKSPACE`** — workspace slug. Not a secret.

Storage locations the script reads from, in order:

1. `~/.config/bb/config` (preferred; user-only file mode recommended)
2. `.env` in the script's directory (gitignored — see `.gitignore`)
3. Process environment variables

The script never writes credentials anywhere. It does emit HTTP requests with
`Authorization: Basic` headers; do not run `bb` with `-v`/`set -x` shell
tracing if your terminal is being recorded, as the token will appear in the
trace.

If you suspect a token has been exposed, rotate it at
<https://id.atlassian.com/manage-profile/security/api-tokens> — the new token
inherits the same permissions and you can paste it into your config.
