# Maintainer checklist

Contributors submit fork pull requests. Only the maintainer merges changes and creates release tags.
Users install tagged releases; `main` is not a release.

## Protect main

- [ ] Keep repository write and admin access with the maintainer only. Do not grant contributors or automation merge access.
- [ ] Merge the [CODEOWNERS file](../.github/CODEOWNERS) and [test workflow](../.github/workflows/test.yml) before you configure protection.
- [ ] Let both CI jobs run once so GitHub knows the `server` and `ui` check names.
- [ ] Review existing protection before you apply the command below; it replaces the supplied settings.
- [ ] Apply protection with a GitHub CLI login that has repository Administration write permission.

Run this command later. It requires pull requests, the `server` and `ui` checks, and one CODEOWNERS approval.
It blocks force pushes and branch deletion. It does not apply the rules to admins, so the maintainer can still push to `main` directly.
Linear history is optional; change `required_linear_history` to `true` to require it.

```bash
gh api --method PUT repos/incmak/tether/branches/main/protection \
  -H 'Accept: application/vnd.github+json' \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["server", "ui"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": true,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

GitHub supports branch push restrictions only for organization-owned repositories.
This personal repository uses `restrictions: null`; the access rule above keeps merges with the maintainer.
CODEOWNERS alone does not limit merge access.
The sole code owner cannot approve their own pull request. The maintainer pushes their own changes to `main` directly, or merges their own pull request as an admin.

See the official [branch protection API](https://docs.github.com/en/rest/branches/branch-protection)
and [GitHub CLI input format](https://cli.github.com/manual/gh_api).

## Approve fork workflows

- [ ] Open repository **Settings → Actions → General**.
- [ ] Under **Approval for running fork pull request workflows from contributors**, select **Require approval for first-time contributors**.
- [ ] Save the setting.
- [ ] Inspect each first-time contributor's changes before you approve the workflow run, especially workflow and dependency changes.

The workflow uses `pull_request`, a read-only token, and no secrets. Keep those properties when you change CI.
Workflow approval permits tests to run; it does not approve or merge the pull request.
See the official [Actions settings guide](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository).

## Review and merge

1. Check that the pull request contains one change and follows [CONTRIBUTING.md](../CONTRIBUTING.md).
2. Check code, tests, fixtures, and screenshots for secrets and personal data.
3. Check tests for behavior changes, screenshots for UI changes, and docs for changed behavior or setup.
4. Keep the Python server standard-library only, with optional extras isolated.
5. Review the diff and approve as the code owner only when the change is ready.
6. Wait for both `server` and `ui` checks to pass on the current branch state.
7. Resolve review comments and merge as the maintainer. Use squash or rebase merge if linear history is enabled.

## Cutting a release

Only the maintainer creates release tags. A push to `main` does not release software to users.

1. Bump the root `VERSION` file, for example to `0.2.0` or `0.2.0-rc1`.
2. Run the server tests and UI checks listed in [AGENTS.md](../AGENTS.md).
3. Commit the release changes to `main` after review.
4. Create the matching tag with `git tag v0.2.0`.
5. Push that tag with `git push origin v0.2.0`.
6. Check the `release` workflow and the published assets.

The tag must equal `v` plus the contents of `VERSION`.
The workflow tests the tagged tree and builds the UI.
It creates `tether-<version>.tar.gz` with the tracked tree and `ui/dist`, plus `SHA256SUMS`.
The archive excludes git metadata, dependencies, and untracked test data.
Only the publication job has `contents: write` permission.
It uses `gh release create` with `GITHUB_TOKEN`, tag verification, and generated release notes.
Tags with `-rc` produce prereleases. Daily stable checks use the latest stable release endpoint.
